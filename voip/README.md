# voip: la SIM del modem come linea del centralino

Container accanto al gateway SMS, sullo stesso modem Quectel (EC25/EG25). Asterisk con
`chan_quectel` fa solo da **ponte**: le chiamate in arrivo sulla SIM vanno al centralino
come trunk SIP, e quelle che il centralino manda al trunk escono dalla SIM. Non ha interni né telefoni.

```
SIM ── modem Quectel ──┬── ModemManager → sms-gateway (SMS, interfaccia AT 03)
                       └── Asterisk + chan_quectel (voce: interfaccia AT 02 + audio USB)
                                   │ trunk SIP "peer" (per IP, senza registrazione)
                                   ▼
                              centralino (Yeastar)
```

- **SMS**: restano al gateway SMS. `chan_quectel` ha gli SMS spenti (`disablesms`) e, alla
  compilazione, non cambia più la memoria e le notifiche degli SMS del modem (`AT+CPMS`/`AT+CNMI`).
- **Sicurezza**: risponde solo all'IP del centralino (ACL e identificazione per IP, nessun
  endpoint anonimo). Il firewall del server deve comunque permettere SIP e RTP solo dal centralino.
- **Riavvii del modem**: porta AT e scheda audio si cercano da sole; se cambiano nome
  (modem riavviato o ricollegato) la configurazione si aggiorna e `chan_quectel` si ricarica.

## Requisiti del modem

1. **VoLTE** (in Italia il 3G è spento): con `ModemManager` in debug (`MM_DEBUG=1` nel gateway SMS)
   `mmcli -m any --command='AT+QCFG="ims"'` deve rispondere `+QCFG: "ims",1,1`.
   Se è `0,…`: `AT+QCFG="ims",1` e riavvio del modem (`AT+CFUN=1,1`).
2. **Audio USB (UAC)**, una volta sola (resta salvato):
   - `AT+QCFG="USBCFG"` → per esempio `+QCFG: "usbcfg",0x2C7C,0x0125,1,1,1,1,1,0,0`
   - rimandare **la stessa risposta con l'ultima cifra a 1**: `AT+QCFG="USBCFG",0x2C7C,0x0125,1,1,1,1,1,0,1`
     (se la risposta ha meno di 7 cifre dopo `0x0125` il firmware non ha l'audio USB);
   - riavvio del modem (`AT+CFUN=1,1`). Sul server, `cat /proc/asound/cards` deve mostrare una
     scheda del modem (di solito `Android`).
   - Firmware EG25-G 30.200: se in chiamata non si sente chi chiama (audio solo in un senso),
     `AT+QAUDMOD=3` (resta salvato); in alternativa aggiornare il firmware del modem.
3. Nel **gateway SMS**: `MM_RESERVED_IFACES=02`, così ModemManager lascia l'interfaccia 02 alla voce
   e usa la 03.

### Voce sulla porta seriale (`AUDIO_MODE=serial`)

In alternativa all'audio USB la voce passa sull'interfaccia 01 del modem (`AT+QPCMV=1,0`, lo manda
`chan_quectel` a ogni chiamata). Quella porta di solito trasmette il GPS (NMEA): va zittita una volta,
`AT+QGPSCFG="outport","none"` (resta salvato). Il gateway SMS la ignora già.

**Volume in ingresso**: con il firmware 30.200 la voce del chiamante arriva quasi muta; `AT+QRXGAIN=16384`
(predefinito 8192) e `AT+CLVL=5` la portano a un livello normale.

## Compose (Dockge)

```yaml
services:
  voip-gsm:
    build: https://github.com/magheggi0/sms-gateway.git#main:voip
    image: voip-gsm:local
    pull_policy: build
    container_name: voip-gsm
    restart: unless-stopped
    network_mode: host
    privileged: true
    volumes:
      - /dev:/dev
    environment:
      YEASTAR_IP: "192.168.1.5"
      GSM_DID: "3XXXXXXXXX"     # numero della SIM: è il numero chiamato che arriva al centralino
```

La prima costruzione compila Asterisk: richiede diversi minuti.

| Variabile        | Predefinito | Significato |
|------------------|-------------|-------------|
| `YEASTAR_IP`     | —           | IP del centralino (obbligatorio): l'unico che può usare il trunk |
| `YEASTAR_PORT`   | `5060`      | Porta SIP del centralino |
| `SIP_PORT`       | `5070`      | Porta SIP di questo ponte |
| `RTP_START`/`RTP_END` | `16000`/`16100` | Porte della voce |
| `GSM_DID`        | `gsm`       | Numero chiamato inviato al centralino per le chiamate entranti (per l'instradamento in entrata) |
| `AT_IFACE`       | `02`        | Interfaccia USB AT del modem usata per la voce (deve essere in `MM_RESERVED_IFACES` del gateway SMS) |
| `AUDIO_MODE`     | `uac`       | Voce sull'audio USB del modem (`uac`) o sulla porta seriale USB (`serial`, interfaccia `AUDIO_IFACE`) |
| `AUDIO_IFACE`    | `01`        | Interfaccia USB della voce in modalità `serial` (sul Quectel la porta NMEA/GPS) |
| `ALSADEV`        | trovata     | Scheda audio del modem in modalità `uac`, es. `plughw:CARD=EG25GC,DEV=0` |
| `WATCH_INTERVAL` | `15`        | Secondi tra un controllo del modem e l'altro |

## Centralino (Yeastar serie P)

**Trunk** (Linee › Trunk › Aggiungi):
- tipo **Peer Trunk**, protocollo SIP, trasporto UDP;
- indirizzo `IP del server` (es. `192.168.1.254`), porta `5070`;
- codec **G.711 A-law** (e u-law); DTMF RFC 2833/4733;
- nessuna registrazione né credenziali (il ponte accetta solo l'IP del centralino).

**Instradamento in entrata**: DID = `GSM_DID` (o «qualsiasi» su questo trunk) → interno, gruppo o IVR.
**Instradamento in uscita**: le chiamate da far uscire dalla SIM verso questo trunk; il numero
esce così com'è (nazionale `3331234567` o internazionale `+39…`).

## Controlli

```sh
docker logs voip-gsm 2>&1 | grep '\[voip\]'
docker exec voip-gsm asterisk -rx 'quectel show devices'     # gsm0: Free = pronto
docker exec voip-gsm asterisk -rx 'pjsip show endpoint yeastar'
```
