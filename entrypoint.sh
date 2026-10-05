#!/bin/bash
set -u

echo "[entrypoint] avvio dbus..."
mkdir -p /run/dbus
rm -f /run/dbus/pid /var/run/dbus/pid
pkill -f dbus-daemon 2>/dev/null || true
sleep 1
dbus-daemon --system --fork || echo "[entrypoint] WARNING: dbus-daemon fallito, continuo comunque"

echo "[entrypoint] preparo database udev (tmpfs privato, niente demone reale)..."
mkdir -p /run/udev/data

# --- trova dinamicamente il device di controllo (cdc-wdm0, cdc-wdm1, ...) ---
MBIM_DEVICE_NAME=""
for i in 0 1 2 3; do
    if [ -e "/dev/cdc-wdm${i}" ]; then
        MBIM_DEVICE_NAME="cdc-wdm${i}"
        break
    fi
done

if [ -z "$MBIM_DEVICE_NAME" ]; then
    echo "[entrypoint] WARNING: nessun /dev/cdc-wdm* trovato"
else
    echo "[entrypoint] device di controllo trovato: ${MBIM_DEVICE_NAME}"

    if [ -r "/sys/class/usbmisc/${MBIM_DEVICE_NAME}/dev" ]; then
        MM_DEVNUM=$(cat "/sys/class/usbmisc/${MBIM_DEVICE_NAME}/dev")
        echo "[entrypoint] ${MBIM_DEVICE_NAME} major:minor = ${MM_DEVNUM}"
        cat > "/run/udev/data/c${MM_DEVNUM}" << EOF
E:ID_MM_CANDIDATE=1
G:seat
EOF
    else
        echo "[entrypoint] WARNING: /sys/class/usbmisc/${MBIM_DEVICE_NAME}/dev non trovato"
    fi

    # --- trova dinamicamente l'interfaccia di rete associata allo stesso
    #     device USB del controllo (il nome NON e' sempre 'wwan0' -
    #     su alcuni sistemi con "predictable network interface names" e'
    #     qualcosa tipo 'wwp7s0u1u4') ---
    USB_SYSPATH=$(readlink -f "/sys/class/usbmisc/${MBIM_DEVICE_NAME}/device" 2>/dev/null || true)

    WWAN_IFACE=""
    WWAN_WAIT=0
    while [ -z "$WWAN_IFACE" ] && [ "$WWAN_WAIT" -lt 15 ]; do
        for net_if in /sys/class/net/*; do
            ifname=$(basename "$net_if")
            [ "$ifname" = "lo" ] && continue
            iface_usb_syspath=$(readlink -f "$net_if/device" 2>/dev/null || true)
            if [ -n "$USB_SYSPATH" ] && [ "$iface_usb_syspath" = "$USB_SYSPATH" ]; then
                WWAN_IFACE="$ifname"
                break
            fi
            # fallback: nome che inizia per "ww" (wwan0, wwp7s0u1u4, ecc.)
            case "$ifname" in
                ww*) WWAN_IFACE="$ifname" ;;
            esac
        done
        if [ -z "$WWAN_IFACE" ]; then
            sleep 1
            WWAN_WAIT=$((WWAN_WAIT + 1))
        fi
    done

    if [ -n "$WWAN_IFACE" ]; then
        echo "[entrypoint] interfaccia WWAN trovata: ${WWAN_IFACE} (dopo ${WWAN_WAIT}s)"
        if [ -r "/sys/class/net/${WWAN_IFACE}/ifindex" ]; then
            MM_IFINDEX=$(cat "/sys/class/net/${WWAN_IFACE}/ifindex")
            echo "[entrypoint] ${WWAN_IFACE} ifindex = ${MM_IFINDEX}"
            cat > "/run/udev/data/n${MM_IFINDEX}" << EOF
E:ID_MM_CANDIDATE=1
G:seat
EOF
        fi
    else
        echo "[entrypoint] WARNING: nessuna interfaccia WWAN trovata dopo ${WWAN_WAIT}s"
    fi
fi

echo "[entrypoint] avvio ModemManager..."
/usr/sbin/ModemManager > /var/log/modemmanager.log 2>&1 &

echo "[entrypoint] attendo comparsa modem (fino a 45s)..."
MODEM=""
WAITED=0
while [ -z "$MODEM" ] && [ "$WAITED" -lt 45 ]; do
    sleep 3
    WAITED=$((WAITED + 3))
    MODEM=$(mmcli -L 2>/dev/null | grep -oP '/Modem/\d+' | head -1 || true)
done

if [ -n "${MODEM:-}" ]; then
    echo "[entrypoint] modem trovato: $MODEM"

    ENABLED=0
    for i in 1 2 3; do
        if mmcli -m "$MODEM" --enable 2>&1 | grep -qi "successfully enabled"; then
            ENABLED=1
            break
        fi
        sleep 3
    done

    if [ "$ENABLED" = "1" ]; then
        echo "[entrypoint] modem abilitato"
    else
        echo "[entrypoint] WARNING: enable fallito, verra' ritentato dall'app al primo utilizzo"
    fi

    mmcli -m "$MODEM" --signal-setup=5 >/dev/null 2>&1 || true
else
    echo "[entrypoint] nessun modem rilevato dopo ${WAITED}s (verra' cercato di nuovo dall'app)"
fi

echo "[entrypoint] avvio Flask..."
exec python3 /app/app.py
