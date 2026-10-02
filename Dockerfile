FROM debian:bookworm

RUN apt update && apt install -y \
    modemmanager \
    libmbim-utils \
    libqmi-utils \
    dbus \
    python3 python3-pip python3-flask \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py /app/app.py
COPY templates /app/templates
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
RUN mkdir -p /data

EXPOSE 5000
CMD ["/entrypoint.sh"]
