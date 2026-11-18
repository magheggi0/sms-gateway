# Linea GSM come trunk SIP: Asterisk + chan_quectel sul modem Quectel (EC25/EG25)
# che usa gia' il gateway SMS. Asterisk fa solo da ponte modem <-> centralino:
# niente interni, niente telefoni.
FROM debian:bookworm

ARG ASTERISK_VERSION=22.11.0
ARG CHAN_QUECTEL_REPO=https://github.com/IchthysMaranatha/asterisk-chan-quectel.git
ARG CHAN_QUECTEL_COMMIT=3d45c7f072131296a7e3c1a4faf5bb18751dbd87

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential autoconf automake git ca-certificates curl bzip2 patch pkg-config \
        libedit-dev libjansson-dev libxml2-dev uuid-dev libsqlite3-dev libssl-dev \
        libsrtp2-dev libasound2-dev alsa-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src

# Asterisk (LTS 22). BUILD_NATIVE spento: l'immagine non dipende dalla CPU su cui si compila.
RUN curl -fsSL "https://github.com/asterisk/asterisk/archive/refs/tags/${ASTERISK_VERSION}.tar.gz" | tar xz \
    && cd "asterisk-${ASTERISK_VERSION}" \
    && ./configure --prefix=/usr --libdir=/usr/lib --sysconfdir=/etc --localstatedir=/var \
         --with-pjproject-bundled >/dev/null \
    && make menuselect.makeopts >/dev/null \
    && menuselect/menuselect --disable BUILD_NATIVE menuselect.makeopts \
    && make -j"$(nproc)" >/dev/null \
    && make install >/dev/null && make install-headers >/dev/null \
    && cd .. && rm -rf "asterisk-${ASTERISK_VERSION}"

# chan_quectel. Gli SMS restano al gateway SMS (ModemManager): all'avvio il modulo
# cambierebbe la memoria degli SMS (AT+CPMS) e le notifiche (AT+CNMI) del modem;
# li sostituiamo con un semplice AT, che risponde OK, cosi' l'inizializzazione prosegue.
# Con il VoLTE la fine chiamata arriva solo come NO CARRIER: patches/no_carrier.inc la chiude.
COPY patches/no_carrier.inc /usr/src/no_carrier.inc
RUN git clone -q "${CHAN_QUECTEL_REPO}" chan_quectel \
    && cd chan_quectel && git checkout -q "${CHAN_QUECTEL_COMMIT}" \
    && test "$(grep -c 'Receive NO CARRIER' at_response.c)" = 1 \
    && sed -i '/Receive NO CARRIER/r /usr/src/no_carrier.inc' at_response.c \
    && grep -q "NO CARRIER chiude le chiamate" at_response.c \
    && sed -i 's|"AT+CPMS=\\"SM\\",\\"SM\\",\\"SM\\"\\r"|"AT\\r"|; s|"AT+CNMI=2,1,0,2,0\\r"|"AT\\r"|' at_command.c \
    && ! grep -q 'AT+CPMS=\\"SM\|AT+CNMI=2,1,0,2,0' at_command.c \
    && ./bootstrap >/dev/null \
    && ./configure --with-astversion="${ASTERISK_VERSION}" --with-asterisk=/usr/include \
         DESTDIR=/usr/lib/asterisk/modules >/dev/null \
    && make >/dev/null && make install \
    && test -f /usr/lib/asterisk/modules/chan_quectel.so \
    && cd .. && rm -rf chan_quectel

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

CMD ["/entrypoint.sh"]
