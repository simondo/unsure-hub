FROM python:3.12-slim

WORKDIR /app
COPY sureflap_bridge.py flapctl.py ./
RUN chmod +x sureflap_bridge.py flapctl.py

ENV SUREFLAP_CONFIG=/data/config.json
VOLUME ["/data"]

EXPOSE 17754/udp 17756/udp 8000/tcp

ENTRYPOINT ["python3", "-u", "/app/sureflap_bridge.py"]
