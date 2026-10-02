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

EXPOSE 5000
CMD ["/entrypoint.sh"]
