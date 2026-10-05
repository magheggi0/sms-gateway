FROM debian:bookworm

RUN apt update && apt install -y \
    modemmanager \
    libmbim-utils \
    libqmi-utils \
    dbus \
    python3 python3-flask python3-waitress \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py auth.py storage.py /app/
COPY templates /app/templates
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
RUN mkdir -p /data
# Dati persistenti (database e chiavi API): montare sempre una cartella su /data.
# Se manca, Docker crea comunque un volume, così i dati non restano nel container.
VOLUME /data

EXPOSE 5000
CMD ["/entrypoint.sh"]
