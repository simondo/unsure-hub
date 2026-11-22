FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir paho-mqtt esptool
COPY sureflap_bridge.py flapctl.py ./
# Optional pre-built ESP32-C5 firmware (populated automatically by CI or local builder)
COPY firmware/ /app/firmware/
RUN chmod +x sureflap_bridge.py flapctl.py

ENV SUREFLAP_CONFIG=/data/config.json
VOLUME ["/data"]

EXPOSE 17754/udp 17756/udp 8000/tcp

ENTRYPOINT ["python3", "-u", "/app/sureflap_bridge.py"]
