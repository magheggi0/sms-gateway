#!/bin/bash
set -u

log() { echo "[entrypoint] $*"; }

# Variabili facoltative:
#   MM_RESERVED_IFACES  interfacce USB del modem lasciate a un altro programma (es. "02")
#   MM_DEBUG=1          ModemManager in modalita' debug (permette mmcli --command='AT...')
#   MM_WATCHDOG=0       spegne il controllo continuo del modem (predefinito: acceso)
#   MM_WATCH_INTERVAL   secondi tra un controllo e l'altro (predefinito 15)

log "avvio dbus..."
mkdir -p /run/dbus
rm -f /run/dbus/pid /var/run/dbus/pid
sleep 1
dbus-daemon --system --fork || log "WARNING: dbus-daemon fallito, continuo comunque"

log "preparo database udev (tmpfs privato, niente demone reale)..."
mkdir -p /run/udev/data

# --- lettura del modem da sysfs (nessuna scrittura) ---
# Imposta: CTRL (cdc-wdmN), CTRL_DEV (major:minor), USB_SYSPATH (interfaccia USB del
# controllo), USB_DEV (dispositivo USB), VENDOR, WWAN_IFACE, IFINDEX, TTYS
# ("nome:interfaccia:major:minor" per ogni ttyUSB dello stesso modem).
scan_modem() {
    local wait_net=${1:-0} i
    CTRL=""; CTRL_DEV=""; USB_SYSPATH=""; USB_DEV=""; VENDOR=""; WWAN_IFACE=""; IFINDEX=""; TTYS=""
    for i in 0 1 2 3 4 5 6 7; do
        if [ -e "/dev/cdc-wdm${i}" ]; then CTRL="cdc-wdm${i}"; break; fi
    done
    [ -n "$CTRL" ] || return 1
    CTRL_DEV=$(cat "/sys/class/usbmisc/${CTRL}/dev" 2>/dev/null || true)
    USB_SYSPATH=$(readlink -f "/sys/class/usbmisc/${CTRL}/device" 2>/dev/null || true)
    [ -n "$USB_SYSPATH" ] && USB_DEV=$(dirname "$USB_SYSPATH")
    VENDOR=$(cat "$USB_DEV/idVendor" 2>/dev/null || true)

    # interfaccia di rete dello stesso device USB (il nome non e' sempre 'wwan0':
    # con i "predictable names" e' tipo 'wwp7s0u1u4i4')
    local waited=0 net_if ifname
    while :; do
        for net_if in /sys/class/net/*; do
            ifname=$(basename "$net_if")
            [ "$ifname" = "lo" ] && continue
            if [ -n "$USB_SYSPATH" ] && [ "$(readlink -f "$net_if/device" 2>/dev/null)" = "$USB_SYSPATH" ]; then
                WWAN_IFACE="$ifname"; break
            fi
            case "$ifname" in ww*) WWAN_IFACE="$ifname" ;; esac   # fallback sul nome
        done
        if [ -n "$WWAN_IFACE" ] || [ "$waited" -ge "$wait_net" ]; then break; fi
        sleep 1; waited=$((waited + 1))
    done
    [ -n "$WWAN_IFACE" ] && IFINDEX=$(cat "/sys/class/net/${WWAN_IFACE}/ifindex" 2>/dev/null || true)

    # ttyUSB dello stesso modem: solo Quectel (vendor 2c7c). Con gli altri modem
    # (es. Huawei MBIM del TrueNAS) le seriali restano come prima, non segnalate.
    local tty name intf num dev
    if [ "$VENDOR" = "2c7c" ]; then
        for tty in /sys/class/tty/ttyUSB*; do
            [ -e "$tty" ] || continue
            name=$(basename "$tty")
            intf=$(readlink -f "$tty/device/.." 2>/dev/null || true)
            [ -n "$intf" ] && [ "$(dirname "$intf")" = "$USB_DEV" ] || continue
            num=$(cat "$intf/bInterfaceNumber" 2>/dev/null || true)
            dev=$(cat "$tty/dev" 2>/dev/null || true)
            [ -n "$dev" ] && TTYS="$TTYS $name:$num:$dev"
        done
    fi
    return 0
}

# Firma del modem: cambia se il modem si riavvia o viene ricollegato
# (nuovi numeri di porta, nuovo ifindex dell'interfaccia di rete).
signature() { echo "$CTRL $CTRL_DEV $IFINDEX$TTYS"; }

# --- scrive le porte del modem nel finto database udev ---
write_ports() {
    rm -f /run/udev/data/c* /run/udev/data/n*
    log "device di controllo: ${CTRL} (${CTRL_DEV:-?})"
    if [ -n "$CTRL_DEV" ]; then
        printf 'E:ID_MM_CANDIDATE=1\nG:seat\n' > "/run/udev/data/c${CTRL_DEV}"
    fi
    if [ -n "$IFINDEX" ]; then
        log "interfaccia WWAN: ${WWAN_IFACE} (ifindex ${IFINDEX})"
        printf 'E:ID_MM_CANDIDATE=1\nG:seat\n' > "/run/udev/data/n${IFINDEX}"
    else
        log "WARNING: nessuna interfaccia WWAN trovata"
    fi
    if [ "$VENDOR" != "2c7c" ]; then
        log "modem non Quectel (vendor ${VENDOR:-?}): porte ttyUSB lasciate come prima"
        return 0
    fi

    # Ruoli come nelle regole ufficiali di ModemManager (77-mm-quectel-port-types):
    # 00 = diagnostica, 01 = GPS (ignorate), 02 = AT principale, 03 = AT secondaria.
    local reserved=" ${MM_RESERVED_IFACES:-} " entry name num dev role
    for entry in $TTYS; do
        name=${entry%%:*}; entry=${entry#*:}; num=${entry%%:*}; dev=${entry#*:}
        role="candidata"
        {
            echo "E:ID_MM_CANDIDATE=1"
            case "$reserved" in
                *" $num "*) echo "E:ID_MM_PORT_IGNORE=1"; role="riservata ad altro programma" ;;
                *)
                    case "$num" in
                        00|01) echo "E:ID_MM_PORT_IGNORE=1"; role="ignorata (diagnostica/GPS)" ;;
                        02) echo "E:ID_MM_PORT_TYPE_AT_PRIMARY=1"; role="AT principale" ;;
                        03)
                            case "$reserved" in
                                *" 02 "*) echo "E:ID_MM_PORT_TYPE_AT_PRIMARY=1"; role="AT principale (02 riservata)" ;;
                                *) echo "E:ID_MM_PORT_TYPE_AT_SECONDARY=1"; role="AT secondaria" ;;
                            esac ;;
                    esac ;;
            esac
            echo "G:seat"
        } > "/run/udev/data/c${dev}"
        log "${name} (interfaccia ${num}, ${dev}): ${role}"
    done
}

# --- ModemManager: avvio e arresto (nel container non c'e' pkill) ---
pids_of() {
    local p
    for p in /proc/[0-9]*; do
        case " $* " in *" $(cat "$p/comm" 2>/dev/null) "*) echo "${p#/proc/}" ;; esac
    done
}

stop_mm() {
    local pids i
    pids=$(pids_of ModemManager qmi-proxy mbim-proxy)
    [ -n "$pids" ] || return 0
    kill $pids 2>/dev/null
    for i in 1 2 3 4 5 6 7 8 9 10; do
        [ -n "$(pids_of ModemManager)" ] || break
        sleep 1
    done
    pids=$(pids_of ModemManager qmi-proxy mbim-proxy)
    [ -n "$pids" ] && kill -9 $pids 2>/dev/null
    return 0
}

start_mm() {
    local args=""
    if [ "${MM_DEBUG:-0}" = "1" ]; then args="--debug"; log "ModemManager in modalita' debug"; fi
    log "avvio ModemManager..."
    /usr/sbin/ModemManager $args > /var/log/modemmanager.log 2>&1 &
}

modem_state() {
    mmcli -m "$1" 2>/dev/null | grep -m1 -oP '\|\s+state: \K\S+' | tr -d "'" || true
}

# Attende il modem, aspetta che finisca l'inizializzazione e lo abilita.
enable_modem() {
    local modem="" waited=0 state i
    log "attendo comparsa modem (fino a 60s)..."
    while [ -z "$modem" ] && [ "$waited" -lt 60 ]; do
        sleep 3; waited=$((waited + 3))
        modem=$(mmcli -L 2>/dev/null | grep -oP '/Modem/\d+' | head -1 || true)
    done
    if [ -z "$modem" ]; then
        log "nessun modem rilevato dopo ${waited}s (verra' cercato di nuovo)"
        return 1
    fi
    log "modem trovato: $modem"

    waited=0
    state=$(modem_state "$modem")
    while [ "$waited" -lt 60 ]; do
        case "$state" in initializing|unknown|"") ;; *) break ;; esac
        sleep 3; waited=$((waited + 3))
        state=$(modem_state "$modem")
    done
    case "$state" in
        locked) log "WARNING: SIM bloccata dal PIN: va inserito o tolto il PIN"; return 1 ;;
        failed) log "WARNING: modem in errore: $(mmcli -m "$modem" 2>/dev/null | grep -m1 -oP 'failed reason: \K.*')"; return 1 ;;
        enabled|searching|registered|connecting|connected) log "modem gia' abilitato (stato ${state})" ;;
        *)
            for i in 1 2 3 4 5; do
                if mmcli -m "$modem" --enable 2>&1 | grep -qi "successfully enabled"; then
                    log "modem abilitato"; state=enabled; break
                fi
                sleep 5
            done
            [ "$state" = "enabled" ] || log "WARNING: enable fallito (stato ${state}), verra' ritentato dall'app al primo utilizzo"
            ;;
    esac
    mmcli -m "$modem" --signal-setup=5 >/dev/null 2>&1 || true
    return 0
}

# --- avvio ---
SIG=""
if scan_modem 15; then
    write_ports
    SIG=$(signature)
else
    log "WARNING: nessun /dev/cdc-wdm* trovato (il modem verra' atteso)"
fi
stop_mm
start_mm
[ -n "$SIG" ] && enable_modem

# --- controllo continuo: riavvio del modem, ricollegamento USB, ModemManager fermo ---
watchdog() {
    local interval=${MM_WATCH_INTERVAL:-15} fails=0 backoff=0 new reason absent=0
    [ -n "$SIG" ] || absent=1
    while sleep "$interval"; do
        if [ "$backoff" -gt 0 ]; then backoff=$((backoff - interval)); continue; fi
        if ! scan_modem 0; then
            [ "$absent" = "1" ] || log "modem scollegato: attendo che ricompaia"
            absent=1; continue
        fi
        new=$(signature)
        reason=""
        if [ "$absent" = "1" ]; then reason="modem comparso"
        elif [ "$new" != "$SIG" ]; then reason="modem riavviato o porte cambiate"
        elif [ -z "$(pids_of ModemManager)" ]; then reason="ModemManager fermo"
        fi
        absent=0
        if [ -z "$reason" ]; then fails=0; continue; fi

        log "${reason}: riconfiguro le porte e riavvio ModemManager"
        sleep 5                       # lascia finire l'enumerazione USB
        scan_modem 15 || { absent=1; continue; }
        write_ports
        SIG=$(signature)
        stop_mm
        start_mm
        if enable_modem; then
            fails=0
        else
            # ModemManager che continua a cadere: aspetta sempre di piu' (max 5 minuti)
            fails=$((fails + 1))
            backoff=$((fails * 60)); [ "$backoff" -gt 300 ] && backoff=300
            log "nuovo tentativo tra ${backoff}s"
        fi
    done
}

if [ "${MM_WATCHDOG:-1}" = "1" ]; then
    log "controllo continuo del modem attivo (ogni ${MM_WATCH_INTERVAL:-15}s)"
    watchdog &
fi

log "avvio Flask..."
exec python3 /app/app.py
