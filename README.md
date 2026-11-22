# SureFlap ESP32-C5 Local Hub

A **$7, 100% local replacement for the SureFlap / Sure Petcare Connect Hub** built on an **ESP32-C5** (dual-band 5 GHz Wi-Fi + IEEE 802.15.4) and a zero-dependency Python bridge for **Home Assistant**.

No cloud account, no official SureFlap Hub, no TLS hacks, and no subscriptions — just direct 802.15.4 / MiWi communication between your ESP32-C5 and your SureFlap Microchip Pet Door / Cat Flap Connect.

```
┌──────────────────────┐   802.15.4 Ch 15 (PAN 0x3421)   ┌────────────────────┐   UDP (5 GHz Wi-Fi)   ┌──────────────────────┐   MQTT Discovery   ┌────────────────┐
│ SureFlap Pet Door /  │ ◄─────────────────────────────► │   ESP32-C5 Board   │ ◄───────────────────► │  sureflap_bridge.py  │ ◄────────────────► │ Home Assistant │
│ Cat Flap Connect     │   5.56s sync + 0x09 cmd window  │  (ESP-IDF v5.5+)   │  17754/17755/17756    │  (Docker / Python)   │   (or REST API)    │  Dashboard     │
└──────────────────────┘                                 └────────────────────┘                       └──────────────────────┘                    └────────────────┘
```

## Features

* **Full two-way local control:** Set lock mode (**Unlocked**, **Keep In**, **Keep Out**, **Locked**, **Curfew**) directly from Home Assistant or the CLI.
* **Native Home Assistant MQTT Auto-Discovery:** Point the Docker container at your MQTT broker — zero HA tokens or YAML helpers needed. Automatically creates a **SureFlap Cat Flap** device with per-pet sensors, battery, curfew, lock-mode `select`, and 1-tap manual in/out Flip buttons (REST API fallback also supported).
* **Onboard curfew programming + clock sync:** Write curfew lock/unlock times directly to the flap's registers (`519–523`) so the flap enforces curfew autonomously even if Wi-Fi or Home Assistant goes down, while still allowing instant HA overrides on top.
* **Real-time pet tracking & telemetry:** Per-pet inside/outside state (including optional `toggle_on_in` mode for Pet Door Connect units that send `0x61` for both entries and normal-speed exits), `0x40` peek (`looked in`) decoding, last event, battery voltage/percentage, flap clock, and curfew state.
* **5 GHz Wi-Fi + 2.4 GHz 802.15.4:** Uses the ESP32-C5's 5 GHz Wi-Fi radio so Wi-Fi traffic never contends with the 2.4 GHz 802.15.4 radio (with automatic fallback to 2.4 GHz + timed 5.56 s RX windows).
* **Auto-learning session keystream:** Automatically recovers the flap's per-session XOR keystream on pair/re-pair and syncs it to the ESP32 via UDP.
* **Multi-arch Docker + Built-in HTTP OTA + Wireshark ZEP v2 stream:** Runs on `linux/amd64`, `linux/arm64`, and `linux/arm/v7`, updates ESP32 firmware wirelessly over HTTP, and streams live 802.15.4 frames to Wireshark on `udp.port == 17754`.

---

## Hardware Required

* Any **ESP32-C5** dev board (e.g., ESP32-C5-DevKitC-1, ~$7) — chosen because it combines **5 GHz Wi-Fi** and **IEEE 802.15.4** on a single chip.
* A **SureFlap Microchip Pet Door Connect** (tested on register-based Pet Door Connect firmware; leave the physical rotary dial in **Position 1 / Unlocked** so the motorized lock pins respond to wireless commands).
* A host (Mac, Linux box, Raspberry Pi, or Docker host) on the same LAN to run `bridge/sureflap_bridge.py`.

---

## Quick Start

### 1. Configure & Flash the ESP32-C5 (`firmware/`)

Requires **ESP-IDF v5.5+** (tested with ESP-IDF v5.5.6):

```bash
cd firmware
cp main/secrets.h.example main/secrets.h
# Edit main/secrets.h with your Wi-Fi SSID/password, bridge host IP (SINK_IP), and EUI-64

idf.py set-target esp32c5
idf.py build flash monitor
```

Note the ESP32-C5's IPv4 address from serial output (or assign a DHCP reservation on your router).

### 2. Start the Local Bridge (`bridge/`)

The bridge requires **Python 3.9+** with **zero third-party packages** (or use Docker):

```bash
cd bridge
cp config.example.json config.json
# Edit config.json with your esp32_ip, Home Assistant URL + Long-Lived Access Token, and pet names

python3 -u sureflap_bridge.py -c config.json
```

Or with Docker Compose:

```bash
cd bridge
mkdir -p data
cp config.example.json data/config.json
# Edit data/config.json
docker compose up -d
```

### 3. Pair the Cat Flap

1. Make sure the ESP32-C5 is powered on and reporting `802.15.4 started on ch 15 PAN 0x3421`.
2. Press the pairing/cog button on your SureFlap flap (or power-cycle its batteries if it isn't paired to another hub).
3. In the bridge log you'll see `beaconreq` → `assoc` → `start` → periodic `online` check-ins every 5.56 seconds.
4. Once the flap sends its first movement + register frames (or you toggle the dial once), the bridge automatically learns the session keystream and sends `setkey` to the ESP32.

### 4. Add Home Assistant Controls (`homeassistant/`)

1. Copy [`homeassistant/packages/sureflap_local.yaml`](homeassistant/packages/sureflap_local.yaml) into your Home Assistant `packages/` directory (or merge the `input_select` and `automation` blocks into `configuration.yaml`) and reload.
2. Add the tiles from [`homeassistant/dashboard-card.yaml`](homeassistant/dashboard-card.yaml) to your dashboard.

### 5. Control & Program Curfew via CLI (`bridge/flapctl.py`)

```bash
# Switch lock mode (unlocked | keepin | keepout | locked | curfew)
./bridge/flapctl.py lock keepin
./bridge/flapctl.py lock curfew

# Program the flap's onboard curfew (e.g. 18:30 locked in -> 06:00 exit allowed) and enable Curfew mode
./bridge/flapctl.py curfew 18:30 06:00 --enable

# Sync the flap's internal clock to local host time
./bridge/flapctl.py synctime
```

---

## Recommended Curfew Setup: Onboard Fallback + HA Control

1. Program your normal daily schedule onto the flap itself (`./bridge/flapctl.py curfew 18:30 06:00 --enable`).
2. Leave the flap in **Curfew** mode — it will lock and unlock autonomously even if your network or Home Assistant is offline.
3. Use `input_select.catflap_lock_mode` in Home Assistant whenever you want an ad-hoc override (`Keep In` before a vet trip, `Unlocked` to let a pet out briefly).
4. Enable the safety-net automation in [`homeassistant/packages/sureflap_local.yaml`](homeassistant/packages/sureflap_local.yaml) so an accidental manual `Unlocked` automatically reverts to `Curfew` at curfew start.

---

## Protocol Documentation

See [`docs/PROTOCOL.md`](docs/PROTOCOL.md) for the full IEEE 802.15.4 / MiWi handshake, XOR keystream auto-learning math, universal checksum invariant (`XOR(plain[4..L-1]) == 0x2C`), and Pet Door Connect register map.

## Credits & License

Protocol register definitions build upon the reverse-engineering work of the [PetHubLocal](https://github.com/plambrechtsen/pethublocal) community. Released under the [MIT License](LICENSE).
