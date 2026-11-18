#!/bin/bash
set -u

log() { echo "[voip] $*"; }

# --- impostazioni (variabili del compose) ---
YEASTAR_IP=${YEASTAR_IP:-}              # centralino che usa la linea (obbligatorio)
YEASTAR_PORT=${YEASTAR_PORT:-5060}      # porta SIP del centralino
SIP_PORT=${SIP_PORT:-5070}              # porta SIP di questo ponte
RTP_START=${RTP_START:-16000}           # porte della voce (RTP)
RTP_END=${RTP_END:-16100}
GSM_DID=${GSM_DID:-gsm}                 # numero chiamato inviato al centralino per le entranti
AT_IFACE=${AT_IFACE:-02}                # interfaccia USB AT del modem lasciata alla voce
ALSADEV=${ALSADEV:-}                    # scheda audio del modem (vuoto = trovata da sola)
WATCH_INTERVAL=${WATCH_INTERVAL:-15}

if ! [[ "$YEASTAR_IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
    log "ERRORE: YEASTAR_IP mancante o non valido (es. 192.168.1.5)"
    exit 1
fi
for v in YEASTAR_PORT SIP_PORT RTP_START RTP_END; do
    [[ "${!v}" =~ ^[0-9]{2,5}$ ]] || { log "ERRORE: $v non valido"; exit 1; }
done
[[ "$GSM_DID" =~ ^[+0-9A-Za-z_-]{1,32}$ ]] || { log "ERRORE: GSM_DID non valido"; exit 1; }

# --- porta AT e scheda audio del modem Quectel (vendor 2c7c) ---
find_at_port() {
    local tty intf
    for tty in /sys/class/tty/ttyUSB*; do
        [ -e "$tty" ] || continue
        intf=$(readlink -f "$tty/device/.." 2>/dev/null) || continue
        [ "$(cat "$(dirname "$intf")/idVendor" 2>/dev/null)" = "2c7c" ] || continue
        [ "$(cat "$intf/bInterfaceNumber" 2>/dev/null)" = "$AT_IFACE" ] || continue
        echo "/dev/$(basename "$tty")"
        return 0
    done
    return 1
}

find_alsa() {
    local c
    if [ -n "$ALSADEV" ]; then echo "$ALSADEV"; return 0; fi
    for c in /proc/asound/card[0-9]*; do
        [ -e "$c/usbid" ] || continue
        case "$(cat "$c/usbid")" in
            2c7c:*) echo "hw:CARD=$(cat "$c/id"),DEV=0"; return 0 ;;
        esac
    done
    return 1
}

# --- configurazione di Asterisk ---
mkdir -p /etc/asterisk /var/lib/asterisk /var/spool/asterisk /var/run/asterisk /var/log/asterisk

cat > /etc/asterisk/asterisk.conf << 'EOF'
[directories](!)
astetcdir => /etc/asterisk
astmoddir => /usr/lib/asterisk/modules
astvarlibdir => /var/lib/asterisk
astdbdir => /var/lib/asterisk
astkeydir => /var/lib/asterisk
astdatadir => /var/lib/asterisk
astagidir => /var/lib/asterisk/agi-bin
astspooldir => /var/spool/asterisk
astrundir => /var/run/asterisk
astlogdir => /var/log/asterisk
astsbindir => /usr/sbin

[options]
verbose = 3
EOF

cat > /etc/asterisk/modules.conf << 'EOF'
[modules]
autoload = yes
; servizi non usati da un ponte modem <-> centralino
noload => res_hep.so
noload => res_hep_pjsip.so
noload => res_hep_rtcp.so
noload => chan_iax2.so
noload => chan_unistim.so
noload => chan_mgcp.so
noload => chan_skinny.so
noload => res_phoneprov.so
noload => res_pjsip_phoneprov_provider.so
EOF

cat > /etc/asterisk/logger.conf << 'EOF'
[general]
[logfiles]
; debug compare solo se acceso a mano (core set debug N chan_quectel.so)
console => notice,warning,error,verbose,debug
EOF

cat > /etc/asterisk/rtp.conf << EOF
[general]
rtpstart = ${RTP_START}
rtpend = ${RTP_END}
EOF

# Solo il centralino puo' parlare con questo ponte: ACL e identificazione per IP,
# nessun endpoint anonimo (le chiamate da altri indirizzi vengono rifiutate).
cat > /etc/asterisk/pjsip.conf << EOF
[global]
type = global
user_agent = GSM-Trunk

[transport-udp]
type = transport
protocol = udp
bind = 0.0.0.0:${SIP_PORT}

[acl-centralino]
type = acl
deny = 0.0.0.0/0.0.0.0
permit = ${YEASTAR_IP}/255.255.255.255

[yeastar]
type = endpoint
transport = transport-udp
context = from-yeastar
disallow = all
allow = alaw,ulaw
aors = yeastar
direct_media = no
rtp_symmetric = yes
force_rport = yes
rewrite_contact = yes
dtmf_mode = rfc4733
send_pai = yes
trust_id_inbound = yes
trust_id_outbound = yes

[yeastar]
type = aor
contact = sip:${YEASTAR_IP}:${YEASTAR_PORT}
qualify_frequency = 60

[yeastar]
type = identify
endpoint = yeastar
match = ${YEASTAR_IP}
EOF

cat > /etc/asterisk/extensions.conf << EOF
[general]
static = yes
writeprotect = yes

; chiamate in arrivo sulla SIM -> centralino (numero chiamato = GSM_DID).
; chan_quectel usa come interno il numero della SIM letto dal modem (AT+CNUM),
; oppure "s" se il modem non lo conosce: tutti vanno allo stesso posto.
[from-gsm]
exten => s,1,Goto(entrata,1)
exten => _X.,1,Goto(entrata,1)
exten => _+X.,1,Goto(entrata,1)
exten => entrata,1,NoOp(GSM in arrivo da \${CALLERID(num)})
 same => n,Dial(PJSIP/${GSM_DID}@yeastar,180)
 same => n,Hangup()
; SMS e USSD restano al gateway SMS
exten => sms,1,Hangup()
exten => ussd,1,Hangup()

; chiamate dal centralino -> uscita dalla SIM
[from-yeastar]
exten => _X.,1,NoOp(GSM in uscita verso \${EXTEN})
 same => n,Dial(Quectel/gsm0/\${EXTEN},180)
 same => n,Hangup()
exten => _+X.,1,NoOp(GSM in uscita verso \${EXTEN})
 same => n,Dial(Quectel/gsm0/\${EXTEN},180)
 same => n,Hangup()
EOF

write_quectel() {
    cat > /etc/asterisk/quectel.conf << EOF
[general]
interval = 15
smsdb = /var/lib/asterisk/smsdb
csmsttl = 600

[defaults]
context = from-gsm
group = 0
rxgain = 0
txgain = 0
autodeletesms = no
resetquectel = no
u2diag = -1
usecallingpres = yes
callingpres = allowed_passed_screen
; gli SMS li gestisce il gateway SMS: chan_quectel non li legge
disablesms = yes
language = en
callwaiting = auto
disable = no
initstate = start
dtmf = relax

[gsm0]
data = $1
quec_uac = 1
alsadev = $2
EOF
}

AT_PORT=$(find_at_port || true)
ALSA=$(find_alsa || true)
[ -n "$AT_PORT" ] || log "WARNING: porta AT del modem (interfaccia ${AT_IFACE}) non trovata: verra' cercata di nuovo"
[ -n "$ALSA" ] || log "WARNING: scheda audio USB del modem non trovata: va attivata l'audio USB (UAC) del modem, vedi README"
write_quectel "${AT_PORT:-/dev/ttyUSB-assente}" "${ALSA:-hw:CARD=Android,DEV=0}"
log "centralino ${YEASTAR_IP}:${YEASTAR_PORT}, SIP su ${SIP_PORT}, RTP ${RTP_START}-${RTP_END}"
log "modem: AT ${AT_PORT:-?}, audio ${ALSA:-?}"

# --- controllo continuo: se il modem si riavvia le porte cambiano nome ---
watchdog() {
    local cur_at=$AT_PORT cur_alsa=$ALSA at alsa
    while sleep "$WATCH_INTERVAL"; do
        at=$(find_at_port || true)
        alsa=$(find_alsa || true)
        [ -n "$at" ] || continue
        if [ "$at" != "$cur_at" ] || { [ -n "$alsa" ] && [ "$alsa" != "$cur_alsa" ]; }; then
            log "modem cambiato: AT ${at}, audio ${alsa:-?}: ricarico chan_quectel"
            write_quectel "$at" "${alsa:-${cur_alsa:-hw:CARD=Android,DEV=0}}"
            cur_at=$at; [ -n "$alsa" ] && cur_alsa=$alsa
            asterisk -rx "quectel reload now" >/dev/null 2>&1 || true
        fi
    done
}
watchdog &

log "avvio Asterisk..."
exec /usr/sbin/asterisk -f -vvv
