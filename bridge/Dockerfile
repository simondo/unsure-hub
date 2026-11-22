FROM python:3.12-slim

WORKDIR /app
# cffi (pulled in by esptool) has no prebuilt armv7 wheel, so compile it there with
# temporary build deps; amd64/arm64 use wheels and skip this.
RUN set -eux; \
    if [ "$(dpkg --print-architecture)" = "armhf" ]; then \
      apt-get update; \
      apt-get install -y --no-install-recommends gcc libc6-dev libffi-dev; \
      pip install --no-cache-dir paho-mqtt==2.1.0 esptool==4.8.1; \
      apt-get purge -y gcc libc6-dev libffi-dev; \
      apt-get autoremove -y; \
      rm -rf /var/lib/apt/lists/*; \
    else \
      pip install --no-cache-dir paho-mqtt==2.1.0 esptool==4.8.1; \
    fi
COPY sureflap_bridge.py flapctl.py ./
# Optional pre-built ESP32-C5 firmware (populated automatically by CI or local builder)
COPY firmware/ /app/firmware/
RUN chmod +x sureflap_bridge.py flapctl.py

ENV SUREFLAP_CONFIG=/data/config.json
VOLUME ["/data"]

EXPOSE 17754/udp 17756/udp 8000/tcp

ENTRYPOINT ["python3", "-u", "/app/sureflap_bridge.py"]
