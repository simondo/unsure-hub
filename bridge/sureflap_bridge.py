#!/usr/bin/env python3
"""All-in-one local bridge for the SureFlap ESP32-C5 Hub.

Responsibilities:
  1. Listens on UDP 17754 (ZEP v2) for optional PCAP capture and UDP 17756 for hub logs.
  2. Auto-learns the per-session XOR keystream whenever the flap pairs/re-pairs, and
     automatically syncs the solved keystream back to the ESP32 via UDP `setkey`.
  3. Decodes SureFlap Pet Door Connect register frames:
       - reg 33  : Battery voltage & flap internal clock (hourly)
       - reg 36  : Lock mode (0=Unlocked, 1=Keep In, 2=Keep Out, 3=Locked, 4=Curfew)
       - reg 40  : Physical lock-out / pin status
       - reg 519 : Onboard curfew state & schedule (HH:MM - HH:MM)
       - reg 528+: Named pet entry/exit by chip slot (528 + 3*(slot-1))
       - reg 621 : Unknown pet exit
       - reg 634 : Hourly curfew telemetry
       - reg 1024: Movement trigger (direction + flap timestamp)
  4. Publishes sensors/events to Home Assistant and watches `input_select.catflap_lock_mode`
     to forward two-way lock changes to the ESP32 over UDP.
  5. Optional built-in HTTP OTA server (disabled by default; enable in config.json).

Zero third-party dependencies (Python 3.9+ stdlib only).
"""

import argparse
import http.server
import json
import os
import re
import select
import socket
import struct
import threading
import time
import urllib.request
from typing import Dict, Optional

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

LOCK_MODES = {0: "Unlocked", 1: "Keep In", 2: "Keep Out", 3: "Locked", 4: "Curfew"}
LOCK_BY_NAME = {v.lower(): k for k, v in LOCK_MODES.items()}
CURFEW_STATES = {1: "off", 2: "on", 3: "paused"}


class SureFlapBridge:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.esp_ip = cfg["esp32_ip"]
        self.esp_port = int(cfg.get("esp32_ctrl_port", 17755))
        self.zep_port = int(cfg.get("zep_listen_port", 17754))
        self.stat_port = int(cfg.get("stat_listen_port", 17756))
        self.state_path = cfg.get("state_file", "./state.json")
        self.log_path = cfg.get("log_file", "")
        self.pcap_path = cfg.get("pcap_file", "")

        ha = cfg.get("home_assistant", {})
        self.ha_url = ha.get("url", "").rstrip("/")
        self.ha_token = os.environ.get("HASS_TOKEN") or ha.get("token", "")
        self.ha_select = ha.get("lock_input_select", "input_select.catflap_lock_mode")
        self.ha_poll_sec = float(ha.get("poll_interval_sec", 2.0))

        mq = cfg.get("mqtt", {})
        self.mqtt_enabled = bool(mq.get("enabled", False))
        self.mqtt_host = mq.get("host", "127.0.0.1")
        self.mqtt_port = int(mq.get("port", 1883))
        self.mqtt_user = mq.get("username", "")
        self.mqtt_pass = os.environ.get("MQTT_PASSWORD") or mq.get("password", "")
        self.mqtt_prefix = mq.get("topic_prefix", "sureflap").strip("/")
        self.mqtt_discovery_prefix = mq.get("discovery_prefix", "homeassistant").strip("/")
        self.mqtt_client = None

        self.pets: Dict[int, str] = {int(k): v for k, v in cfg.get("pets", {}).items()}
        self.toggle_on_in: bool = bool(cfg.get("toggle_on_in", False))
        cf = cfg.get("curfew_fallback", {})
        self.fallback_lock = cf.get("lock_time", "18:30")
        self.fallback_unlock = cf.get("unlock_time", "06:00")

        self.state = self._load_state()
        self.sessions: Dict[str, Dict[int, int]] = {
            h: {int(i): v for i, v in kd.items()}
            for h, kd in self.state.get("sessions", {}).items()
        }
        self.chk12: Dict[str, int] = {
            h: int(v) for h, v in self.state.get("chk12", {}).items()
        }
        self.active_hdr: Optional[str] = self.state.get("active_hdr")
        self.seen_dedup: Dict[bytes, float] = {}
        self.last24_time: Optional[str] = None
        self.last24_dir: Optional[int] = None
        self.last_ha_lock: Optional[str] = None
        self.last_flip_ts: Dict[str, str] = {}
        self.pending_unknown_exits: int = 0
        self.cmd_status = "idle"

        self.udp_tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.log_fp = open(self.log_path, "a", buffering=1) if self.log_path else None
        self.pcap_fp = None
        if self.pcap_path:
            self.pcap_fp = open(self.pcap_path, "wb")
            # DLT 195 = IEEE 802.15.4 with FCS
            self.pcap_fp.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 195))
            self.pcap_fp.flush()

    def _load_state(self) -> dict:
        if os.path.isfile(self.state_path):
            try:
                with open(self.state_path) as f:
                    return json.load(f)
            except Exception as e:
                print(f"[warn] could not load {self.state_path}: {e}", flush=True)
        return {
            "location": {},
            "since": {},
            "last_event": None,
            "lock_mode": "Curfew",
            "battery": None,
            "curfew": None,
            "flap_curfew": None,
            "sessions": {},
            "chk12": {},
            "active_hdr": None,
        }

    def _save_state(self) -> None:
        self.state["sessions"] = {
            h: {str(i): v for i, v in sorted(kd.items())}
            for h, kd in self.sessions.items()
        }
        self.state["chk12"] = self.chk12
        self.state["active_hdr"] = self.active_hdr
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp, self.state_path)

    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        if self.log_fp:
            self.log_fp.write(line + "\n")

    def send_esp(self, cmd: str) -> None:
        self.udp_tx.sendto(cmd.encode(), (self.esp_ip, self.esp_port))
        self.log(f"UDP -> {cmd}")

    def push_key_to_esp(self, hdr: str) -> None:
        k = self.sessions.get(hdr, {})
        c12 = self.chk12.get(hdr)
        if c12 is None or not all(i in k for i in range(4, 12)):
            return
        khex = bytes(k[i] for i in range(4, 12)).hex()
        self.send_esp(f"setkey {hdr} {khex} {c12:02x}")

    # ---- MQTT + Home Assistant Discovery helpers ----

    def _mqtt_pub(self, subtopic: str, payload, retain: bool = True) -> None:
        if not self.mqtt_client:
            return
        topic = subtopic if "/" in subtopic and subtopic.startswith(self.mqtt_discovery_prefix + "/") else f"{self.mqtt_prefix}/{subtopic}"
        data = json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)
        self.mqtt_client.publish(topic, data, qos=1, retain=retain)

    def _publish_mqtt_discovery(self) -> None:
        if not self.mqtt_client:
            return
        dev = {
            "identifiers": ["sureflap_esp32_hub"],
            "name": "SureFlap Cat Flap",
            "manufacturer": "SureFlap (ESP32-C5 Local Hub)",
            "model": "Pet Door Connect",
        }
        avail = {
            "availability_topic": f"{self.mqtt_prefix}/status",
            "payload_available": "online",
            "payload_not_available": "offline",
        }
        dp = self.mqtt_discovery_prefix
        p = self.mqtt_prefix

        configs = [
            (f"{dp}/sensor/sureflap_curfew/config", {
                "name": "Curfew",
                "unique_id": "sureflap_catflap_curfew",
                "default_entity_id": "sensor.catflap_curfew",
                "state_topic": f"{p}/curfew/state",
                "json_attributes_topic": f"{p}/curfew/attrs",
                "icon": "mdi:lock-clock",
                "device": dev, **avail,
            }),
            (f"{dp}/sensor/sureflap_last_event/config", {
                "name": "Last event",
                "unique_id": "sureflap_catflap_last_event",
                "default_entity_id": "sensor.catflap_last_event",
                "state_topic": f"{p}/last_event/state",
                "json_attributes_topic": f"{p}/last_event/attrs",
                "icon": "mdi:door",
                "device": dev, **avail,
            }),
            (f"{dp}/sensor/sureflap_lock_mode/config", {
                "name": "Lock state",
                "unique_id": "sureflap_catflap_lock_mode",
                "default_entity_id": "sensor.catflap_lock_mode",
                "state_topic": f"{p}/lock_mode/state",
                "json_attributes_topic": f"{p}/lock_mode/attrs",
                "icon": "mdi:lock-check",
                "device": dev, **avail,
            }),
            (f"{dp}/sensor/sureflap_battery/config", {
                "name": "Battery",
                "unique_id": "sureflap_catflap_battery",
                "default_entity_id": "sensor.catflap_battery",
                "state_topic": f"{p}/battery/state",
                "json_attributes_topic": f"{p}/battery/attrs",
                "device_class": "voltage",
                "state_class": "measurement",
                "unit_of_measurement": "V",
                "icon": "mdi:battery",
                "device": dev, **avail,
            }),
            (f"{dp}/select/sureflap_lock_mode/config", {
                "name": "Lock mode",
                "unique_id": "sureflap_catflap_lock_select",
                "default_entity_id": "select.catflap_lock_mode",
                "state_topic": f"{p}/lock_mode/state",
                "command_topic": f"{p}/lock_mode/set",
                "options": list(LOCK_MODES.values()),
                "icon": "mdi:door-closed-lock",
                "device": dev, **avail,
            }),
        ]
        for pet in self.pets.values():
            pl = pet.lower()
            configs.append((f"{dp}/sensor/sureflap_pet_{pl}/config", {
                "name": pet,
                "unique_id": f"sureflap_catflap_{pl}",
                "default_entity_id": f"sensor.catflap_{pl}",
                "state_topic": f"{p}/pet/{pl}/state",
                "json_attributes_topic": f"{p}/pet/{pl}/attrs",
                "icon": "mdi:cat",
                "device": dev, **avail,
            }))
            configs.append((f"{dp}/button/sureflap_flip_{pl}/config", {
                "name": f"Flip {pet}",
                "unique_id": f"sureflap_catflap_flip_{pl}",
                "default_entity_id": f"button.catflap_flip_{pl}",
                "command_topic": f"{p}/pet/{pl}/flip",
                "icon": "mdi:swap-horizontal",
                "device": dev, **avail,
            }))

        for topic, cfg in configs:
            self._mqtt_pub(topic, cfg, retain=True)
        self.log(f"Published {len(configs)} HA MQTT Discovery configs")

    def _flip_pet(self, pet: str) -> None:
        loc = self.state.setdefault("location", {})
        since = self.state.setdefault("since", {})
        new_loc = "outside" if loc.get(pet) == "inside" else "inside"
        t = time.strftime("%H:%M:%S")
        loc[pet] = new_loc
        since[pet] = t
        self._save_state()
        self.log(f"MANUAL FLIP: {pet} -> {new_loc}")
        self._push_pet(pet, new_loc, t, inferred=False)

    def _set_lock_from_ha(self, val: str) -> None:
        code = LOCK_BY_NAME.get(val.strip().lower())
        if code is None:
            return
        self.cmd_status = f"sending {LOCK_MODES[code]}"
        self.send_esp(f"setlock {code}")
        self.push_lock_sensor()

    def _start_mqtt(self) -> None:
        if mqtt is None:
            raise RuntimeError("paho-mqtt is required when mqtt.enabled is true")
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sureflap-esp32-bridge")
        if self.mqtt_user:
            client.username_pw_set(self.mqtt_user, self.mqtt_pass)
        client.will_set(f"{self.mqtt_prefix}/status", "offline", qos=1, retain=True)

        def on_connect(c, userdata, flags, reason_code, properties=None):
            self.log(f"MQTT connected to {self.mqtt_host}:{self.mqtt_port} ({reason_code})")
            c.publish(f"{self.mqtt_prefix}/status", "online", qos=1, retain=True)
            c.subscribe(f"{self.mqtt_prefix}/lock_mode/set", qos=1)
            c.subscribe(f"{self.mqtt_prefix}/pet/+/flip", qos=1)
            c.subscribe(f"{self.mqtt_discovery_prefix}/status", qos=1)
            self._publish_mqtt_discovery()
            self.push_all_ha()

        def on_message(c, userdata, msg):
            topic = msg.topic
            payload = msg.payload.decode("utf-8", errors="replace").strip()
            if topic == f"{self.mqtt_prefix}/lock_mode/set":
                self.log(f"MQTT lock_mode/set -> {payload}")
                self._set_lock_from_ha(payload)
            elif topic.startswith(f"{self.mqtt_prefix}/pet/") and topic.endswith("/flip"):
                pl = topic.split("/")[-2].lower()
                for pet in self.pets.values():
                    if pet.lower() == pl:
                        self._flip_pet(pet)
                        break
            elif topic == f"{self.mqtt_discovery_prefix}/status" and payload == "online":
                self._publish_mqtt_discovery()
                self.push_all_ha()

        client.on_connect = on_connect
        client.on_message = on_message
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.connect(self.mqtt_host, self.mqtt_port, keepalive=60)
        client.loop_start()
        self.mqtt_client = client

    def _push_pet(self, pet: str, loc: str, since_t: Optional[str], inferred: bool = False) -> None:
        attrs = {
            "since": since_t,
            "friendly_name": pet,
            "inferred": inferred,
            "icon": "mdi:cat" if loc == "inside" else "mdi:tree",
        }
        self._mqtt_pub(f"pet/{pet.lower()}/state", loc, retain=True)
        self._mqtt_pub(f"pet/{pet.lower()}/attrs", attrs, retain=True)
        self.ha_post(f"/api/states/sensor.catflap_{pet.lower()}", {"state": loc, "attributes": attrs})

    # ---- Home Assistant REST helpers ----

    def ha_post(self, path: str, body: dict) -> Optional[bytes]:
        if not self.ha_url or not self.ha_token or "REPLACE_WITH" in self.ha_token:
            return None
        req = urllib.request.Request(
            self.ha_url + path,
            json.dumps(body).encode(),
            {"Authorization": "Bearer " + self.ha_token, "Content-Type": "application/json"},
        )
        try:
            return urllib.request.urlopen(req, timeout=5).read()
        except Exception as e:
            self.log(f"HA POST {path} failed: {e}")
            return None

    def ha_get(self, path: str) -> Optional[dict]:
        if not self.ha_url or not self.ha_token or "REPLACE_WITH" in self.ha_token:
            return None
        req = urllib.request.Request(
            self.ha_url + path,
            headers={"Authorization": "Bearer " + self.ha_token},
        )
        try:
            return json.loads(urllib.request.urlopen(req, timeout=5).read())
        except Exception:
            return None

    def _curfew_window(self):
        c = self.state.get("curfew")
        if c and c.get("lock_time") and c.get("unlock_time"):
            return c["lock_time"], c["unlock_time"]
        return self.fallback_lock, self.fallback_unlock

    def _in_curfew_now(self, hhmm: str) -> bool:
        fc = self.state.get("flap_curfew")
        if fc in ("locked", "open"):
            return fc == "locked"
        lo, hi = self._curfew_window()
        return (hhmm >= lo or hhmm < hi) if lo > hi else (lo <= hhmm < hi)

    def push_all_ha(self) -> None:
        hhmm = time.strftime("%H:%M")
        lo, hi = self._curfew_window()
        locked = self._in_curfew_now(hhmm)
        c_info = self.state.get("curfew") or {}
        curfew_state = "locked" if locked else "open"
        curfew_attrs = {
            "friendly_name": "Cat flap curfew",
            "icon": "mdi:lock-clock" if locked else "mdi:lock-open-variant",
            "lock_time": lo,
            "unlock_time": hi,
            "until": hi if locked else None,
            "locks_at": lo if not locked else None,
            "flap_curfew_state": c_info.get("state", "unknown"),
            "detail": "Keep in: pets can re-enter but not exit" if locked else "Pets can come and go",
        }
        self._mqtt_pub("curfew/state", curfew_state, retain=True)
        self._mqtt_pub("curfew/attrs", {k: v for k, v in curfew_attrs.items() if v is not None}, retain=True)
        self.ha_post("/api/states/sensor.catflap_curfew", {"state": curfew_state, "attributes": curfew_attrs})
        self.push_lock_sensor()
        self.push_battery_sensor()
        ev = self.state.get("last_event")
        if ev:
            ev_str = self._fmt_event(ev["pet"], ev["direction"])
            ev_attrs = {**ev, "friendly_name": "Cat flap last event", "icon": "mdi:door"}
            self._mqtt_pub("last_event/state", ev_str, retain=True)
            self._mqtt_pub("last_event/attrs", ev_attrs, retain=True)
            self.ha_post("/api/states/sensor.catflap_last_event", {"state": ev_str, "attributes": ev_attrs})
        for pet, loc in self.state.get("location", {}).items():
            self._push_pet(pet, loc, self.state.get("since", {}).get(pet))

    def push_lock_sensor(self) -> None:
        mode = self.state.get("lock_mode", "Unknown")
        attrs = {
            "friendly_name": "Cat flap lock mode",
            "icon": "mdi:door-closed-lock",
            "command_status": self.cmd_status,
            "curfew_schedule": self.state.get("curfew"),
        }
        self._mqtt_pub("lock_mode/state", mode, retain=True)
        self._mqtt_pub("lock_mode/attrs", attrs, retain=True)
        self.ha_post("/api/states/sensor.catflap_lock_mode", {"state": mode, "attributes": attrs})

    def push_battery_sensor(self) -> None:
        batt = self.state.get("battery")
        if not batt:
            return
        attrs = {
            "friendly_name": "Cat flap battery",
            "unit_of_measurement": "V",
            "device_class": "voltage",
            "state_class": "measurement",
            "icon": "mdi:battery" if batt["pct"] > 25 else "mdi:battery-alert",
            "battery_pct": batt["pct"],
            "adc": batt["adc"],
            "flap_clock": batt.get("clock"),
            "updated": batt.get("time"),
        }
        self._mqtt_pub("battery/state", batt["volts"], retain=True)
        self._mqtt_pub("battery/attrs", attrs, retain=True)
        self.ha_post("/api/states/sensor.catflap_battery", {"state": batt["volts"], "attributes": attrs})

    def sync_ha_select(self, mode_name: str) -> None:
        self.last_ha_lock = mode_name
        self.ha_post("/api/services/input_select/select_option", {
            "entity_id": self.ha_select,
            "option": mode_name,
        })

    def poll_ha_lock(self) -> None:
        st = self.ha_get(f"/api/states/{self.ha_select}")
        if st and "state" in st:
            val = st["state"]
            if self.last_ha_lock is None:
                self.last_ha_lock = val
            elif val != self.last_ha_lock:
                self.last_ha_lock = val
                code = LOCK_BY_NAME.get(val.lower())
                if code is not None:
                    self.cmd_status = f"sending {val}"
                    self.send_esp(f"setlock {code}")
                    self.push_lock_sensor()

        # Optional 1-tap manual flip buttons (input_button.catflap_flip_<pet>)
        for pet in self.pets.values():
            eid = f"input_button.catflap_flip_{pet.lower()}"
            bst = self.ha_get(f"/api/states/{eid}")
            if not bst or "state" not in bst:
                continue
            ts = bst["state"]
            if eid not in self.last_flip_ts:
                self.last_flip_ts[eid] = ts
            elif ts != self.last_flip_ts[eid] and ts not in ("unknown", "unavailable"):
                self.last_flip_ts[eid] = ts
                loc = self.state.setdefault("location", {})
                since = self.state.setdefault("since", {})
                new_loc = "outside" if loc.get(pet) == "inside" else "inside"
                t = time.strftime("%H:%M:%S")
                loc[pet] = new_loc
                since[pet] = t
                self._save_state()
                self.log(f"MANUAL FLIP: {pet} -> {new_loc}")
                self.ha_post(f"/api/states/sensor.catflap_{pet.lower()}", {
                    "state": new_loc,
                    "attributes": {
                        "since": t,
                        "friendly_name": pet,
                        "inferred": False,
                        "icon": "mdi:cat" if new_loc == "inside" else "mdi:tree",
                    },
                })

    # ---- Protocol decoding & keystream auto-learning ----

    def _fmt_event(self, pet: str, direction: str) -> str:
        if pet == "unknown":
            return {"out": "A pet went out", "in": "A pet came in",
                    "tried to go out": "A pet tried to go out",
                    "looked in": "A pet looked in"}.get(direction, f"A pet {direction}")
        return f"{pet} {direction}"

    def _emit_pet(self, t: str, direction: str, slot: Optional[int], flap_time: str) -> None:
        pet = self.pets.get(slot, f"slot_{slot}" if slot is not None else "unknown")
        inferred = False
        if direction == "out" and self._in_curfew_now(t[:5]) and self.state.get("lock_mode") in ("Curfew", "Keep In", "Locked"):
            direction = "tried to go out"
        loc = self.state.setdefault("location", {})
        since = self.state.setdefault("since", {})
        if slot is None:
            want = "outside" if direction == "in" else "inside"
            cands = [p for p, where in loc.items() if where == want]
            if len(cands) == 1:
                pet, inferred = cands[0], True
            elif direction == "out":
                self.pending_unknown_exits = min(self.pending_unknown_exits + 1, max(1, len(self.pets)))
        if pet != "unknown" and direction in ("in", "out"):
            loc[pet] = "outside" if direction == "out" else "inside"
            since[pet] = t
        ev = {"pet": pet, "direction": direction, "flap_time": flap_time, "inferred": inferred, "time": t}
        self.state["last_event"] = ev
        self._save_state()
        ev_str = self._fmt_event(pet, direction)
        self.log(f"EVENT: {ev_str} (flap {flap_time}{', inferred' if inferred else ''})")
        ev_attrs = {**ev, "friendly_name": "Cat flap last event", "icon": "mdi:door"}
        self._mqtt_pub("event", ev, retain=False)
        self._mqtt_pub("last_event/state", ev_str, retain=True)
        self._mqtt_pub("last_event/attrs", ev_attrs, retain=True)
        self.ha_post("/api/events/catflap_event", ev)
        self.ha_post("/api/states/sensor.catflap_last_event", {"state": ev_str, "attributes": ev_attrs})
        if pet != "unknown" and direction in ("in", "out"):
            self._push_pet(pet, loc.get(pet, "unknown"), t, inferred=inferred)

    def _learn_key(self, hdr: str, b: bytes, t: str) -> Optional[Dict[int, int]]:
        k = self.sessions.setdefault(hdr, {})
        changed = False
        if 4 not in k:
            k[4] = b[4] ^ (len(b) - 2)
            k[5] = b[5] ^ 0x84
            k[8] = b[8]
            changed = True
        if len(b) == 24:
            # 24B movement trigger is always reg 1024 (0x0400), rlen = 13, byte 11=HH, byte 12=MM
            for idx, plain in ((6, 0x04), (7, 0x00), (9, 13), (11, int(t[:2])), (12, int(t[3:5]))):
                if idx not in k:
                    k[idx] = b[idx] ^ plain
                    changed = True
        if len(b) == 12:
            xr = 0
            for v in b[4:]:
                xr ^= v
            if self.chk12.get(hdr) != xr:
                self.chk12[hdr] = xr
                changed = True
            # If k[4..9] and k[11] are known, solve k[10] from XOR(plain[4..11]) == 0x2c
            if all(i in k for i in (4, 5, 6, 7, 8, 9, 11)) and 10 not in k:
                # k[10] = XOR(k[4..9], k[11]) ^ chk12 ^ 0x2c
                kx = xr ^ 0x2C
                for i in (4, 5, 6, 7, 8, 9, 11):
                    kx ^= k[i]
                k[10] = kx
                changed = True
        if changed:
            self.active_hdr = hdr
            self._save_state()
            self.log(f"Learned session {hdr}: keys={sorted(k.keys())} chk12={self.chk12.get(hdr)}")
            self.push_key_to_esp(hdr)
        return k if all(i in k for i in (4, 5, 6, 7, 8, 9)) else None

    def handle_stat_line(self, line: str) -> None:
        self.log(line)
        if "txcmd-ack" in line:
            self.cmd_status = "acked"
            self.push_lock_sensor()
            return
        if "txcmd " in line:
            self.cmd_status = "transmitted"
            self.push_lock_sensor()
            return

        m = re.search(r"HUB .* data ([0-9a-f]+)$", line)
        if not m:
            return
        b = bytes.fromhex(m.group(1))
        if len(b) < 12 or b[0] != 0x01:
            return
        t = time.strftime("%H:%M:%S")
        hdr = b[2:4].hex()
        k = self._learn_key(hdr, b, t)
        if not k or b[4] != ((len(b) - 2) ^ k[4]):
            return

        reg = ((b[6] ^ k[6]) << 8) | (b[7] ^ k[7])
        rlen = b[9] ^ k[9]

        # 24B movement trigger (reg 1024, rlen 13)
        if len(b) == 24 and reg == 1024 and rlen == 13:
            hr = (b[11] ^ k[11]) if 11 in k else 99
            mn = (b[12] ^ k[12]) if 12 in k else 99
            self.last24_time = f"{hr:02d}:{mn:02d}" if hr < 24 and mn < 60 else "??:??"
            self.last24_dir = (b[13] ^ k[13]) if 13 in k else None
            return

        # 5s dedup per body
        now = time.time()
        if (now - self.seen_dedup.get(b[4:], -999)) < 5.0:
            return
        self.seen_dedup[b[4:]] = now

        # 12B Lock Mode (reg 36)
        if len(b) == 12 and reg == 36 and rlen == 1 and 10 in k:
            code = b[10] ^ k[10]
            if code in LOCK_MODES:
                mode = LOCK_MODES[code]
                self.state["lock_mode"] = mode
                self.cmd_status = "confirmed"
                self._save_state()
                self.log(f"LOCK MODE -> {mode} (code {code})")
                self.push_lock_sensor()
                self.sync_ha_select(mode)
            return

        # 14B/16B Battery + Clock (reg 33)
        if reg == 33 and rlen in (3, 5) and 10 in k and 11 in k:
            adc = b[10] ^ k[10]
            hr = b[11] ^ k[11]
            mn = ((b[12] ^ k[12]) & 0x7F) if 12 in k else 0
            volts = round(2.1075 + adc * 0.0225, 2)
            pct = max(0, min(100, round((volts - 4.4) / 1.6 * 100)))
            clock = f"{hr:02d}:{mn:02d}" if hr < 24 and mn < 60 else None
            self.state["battery"] = {"volts": volts, "pct": pct, "adc": adc, "clock": clock, "time": t}
            self.log(f"BATTERY {volts:.2f}V ({pct}%, adc={adc}) clock={clock}")
            if rlen == 5 and len(b) >= 16 and 13 in k:
                code = b[13] ^ k[13]
                if code in LOCK_MODES:
                    self.state["lock_mode"] = LOCK_MODES[code]
                    self.push_lock_sensor()
            self._save_state()
            self.push_battery_sensor()
            return

        # 17B Curfew Schedule (reg 519, rlen 6)
        if len(b) == 17 and reg == 519 and rlen == 6 and all(i in k for i in (10, 11, 12, 13)):
            st_code = b[10] ^ k[10]
            fh, fm, th = b[11] ^ k[11], b[12] ^ k[12], b[13] ^ k[13]
            tm = (b[14] ^ k[14]) if 14 in k else 0
            if fh < 24 and fm < 60 and th < 24 and tm < 60:
                self.state["curfew"] = {
                    "state_code": st_code,
                    "state": CURFEW_STATES.get(st_code, str(st_code)),
                    "lock_time": f"{fh:02d}:{fm:02d}",
                    "unlock_time": f"{th:02d}:{tm:02d}",
                }
                self._save_state()
                self.log(f"CURFEW -> {self.state['curfew']['state']} ({fh:02d}:{fm:02d} -> {th:02d}:{tm:02d})")
                self.push_all_ha()
            return

        # 27B Hourly Curfew Telemetry (reg 634)
        if len(b) == 27 and reg == 634 and rlen == 16 and 10 in k:
            val = b[10] ^ k[10]
            self.state["flap_curfew"] = "open" if val == 0x02 else "locked"
            self._save_state()
            self.push_all_ha()
            return

        # 14B Named pet follow-up (reg 528 + 3*(slot-1))
        if len(b) == 14 and reg >= 528 and (reg - 528) % 3 == 0 and rlen == 3:
            slot = ((reg - 528) // 3) + 1
            pet = self.pets.get(slot, f"slot_{slot}")
            dir_byte = self.last24_dir if self.last24_dir is not None else ((b[12] ^ k[12]) if 12 in k else 0x61)
            low = dir_byte & 0x0F
            if low in (0, 5):
                direction = "looked in"
            elif low == 2:
                direction = "out"
            elif self.toggle_on_in:
                blocked = self.state.get("lock_mode") in ("Keep In", "Locked") or (
                    self.state.get("lock_mode") == "Curfew" and self._in_curfew_now(t[:5])
                )
                if blocked:
                    direction = "in"
                    self.pending_unknown_exits = 0
                elif self.state.get("location", {}).get(pet) == "inside":
                    if self.pending_unknown_exits > 0:
                        self.pending_unknown_exits -= 1
                        direction = "in"
                    else:
                        direction = "out"
                else:
                    direction = "in"
            else:
                direction = "in"
            self._emit_pet(t, direction, slot, self.last24_time or "??:??")
            return

        # 20B Unknown pet exit (reg 621)
        if len(b) == 20 and reg == 621 and rlen == 9 and 10 in k and 11 in k:
            hr, mn = b[10] ^ k[10], b[11] ^ k[11]
            if hr < 24:
                self._emit_pet(t, "out", None, f"{hr:02d}:{mn:02d}" if mn < 60 else f"{hr:02d}:??")

    def _start_ota_server(self) -> None:
        ota_dir = self.cfg.get("ota_dir", "./ota")
        ota_port = int(self.cfg.get("ota_port", 8000))
        os.makedirs(ota_dir, exist_ok=True)
        bridge = self

        class OtaHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):
                fpath = os.path.join(ota_dir, os.path.basename(self.path))
                if not os.path.isfile(fpath):
                    self.send_response(404)
                    self.end_headers()
                    return
                data = open(fpath, "rb").read()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.flush()
                time.sleep(0.2)
                self.wfile.write(data)

            def log_message(self, fmt, *args):
                bridge.log("OTA " + (fmt % args))

        srv = http.server.ThreadingHTTPServer(("0.0.0.0", ota_port), OtaHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.log(f"OTA HTTP server listening on :{ota_port} serving {ota_dir}")

    def run(self) -> None:
        if self.cfg.get("ota_enabled"):
            self._start_ota_server()
        if self.mqtt_enabled:
            self._start_mqtt()

        z_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        z_sock.bind(("0.0.0.0", self.zep_port))
        s_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s_sock.bind(("0.0.0.0", self.stat_port))

        self.log(f"Bridge listening on UDP {self.zep_port} (ZEP) and {self.stat_port} (status) -> ESP32 {self.esp_ip}:{self.esp_port}")
        if self.active_hdr:
            self.push_key_to_esp(self.active_hdr)

        last_poll = 0.0
        last_push = 0.0
        while True:
            rlist, _, _ = select.select([z_sock, s_sock], [], [], 0.5)
            for sock in rlist:
                data, _ = sock.recvfrom(4096)
                if sock is s_sock:
                    self.handle_stat_line(data.decode("utf-8", errors="replace").strip())
                elif self.pcap_fp and len(data) > 32 and data[:2] == b"EX":
                    frame = data[32 : 32 + data[31]]
                    ts = time.time()
                    sec, usec = int(ts), int((ts % 1) * 1_000_000)
                    self.pcap_fp.write(struct.pack("<IIII", sec, usec, len(frame), len(frame)) + frame)
                    self.pcap_fp.flush()

            now = time.time()
            if now - last_poll >= self.ha_poll_sec:
                last_poll = now
                self.poll_ha_lock()
            if now - last_push >= 300.0:
                last_push = now
                self.push_all_ha()


def _load_config(path: str) -> dict:
    if path and os.path.isfile(path):
        with open(path) as f:
            cfg = json.load(f)
    else:
        cfg = {}

    # Allow pure environment-variable configuration for zero-file Docker deployments
    if os.environ.get("ESP32_IP"):
        cfg["esp32_ip"] = os.environ["ESP32_IP"]
    if os.environ.get("STATE_FILE"):
        cfg["state_file"] = os.environ["STATE_FILE"]
    if os.environ.get("LOG_FILE"):
        cfg["log_file"] = os.environ["LOG_FILE"]
    if os.environ.get("TOGGLE_ON_IN"):
        cfg["toggle_on_in"] = os.environ["TOGGLE_ON_IN"].lower() in ("1", "true", "yes")
    if os.environ.get("PETS"):
        pets = {}
        for pair in os.environ["PETS"].split(","):
            if ":" in pair:
                k, v = pair.split(":", 1)
                pets[k.strip()] = v.strip()
        if pets:
            cfg["pets"] = pets
    if os.environ.get("CURFEW_LOCK") or os.environ.get("CURFEW_UNLOCK"):
        cf = cfg.setdefault("curfew_fallback", {})
        if os.environ.get("CURFEW_LOCK"):
            cf["lock_time"] = os.environ["CURFEW_LOCK"]
        if os.environ.get("CURFEW_UNLOCK"):
            cf["unlock_time"] = os.environ["CURFEW_UNLOCK"]
    if os.environ.get("MQTT_HOST"):
        mq = cfg.setdefault("mqtt", {})
        mq["enabled"] = True
        mq["host"] = os.environ["MQTT_HOST"]
        if os.environ.get("MQTT_PORT"):
            mq["port"] = int(os.environ["MQTT_PORT"])
        if os.environ.get("MQTT_USER"):
            mq["username"] = os.environ["MQTT_USER"]
    if os.environ.get("OTA_ENABLED"):
        cfg["ota_enabled"] = os.environ["OTA_ENABLED"].lower() in ("1", "true", "yes")
    if not cfg.get("esp32_ip"):
        raise SystemExit("Config error: set esp32_ip in config.json or ESP32_IP env var")
    return cfg


def main():
    ap = argparse.ArgumentParser(description="SureFlap ESP32-C5 Local Bridge")
    ap.add_argument("-c", "--config", default=os.environ.get("SUREFLAP_CONFIG", "config.json"))
    args = ap.parse_args()
    cfg = _load_config(args.config)
    SureFlapBridge(cfg).run()


if __name__ == "__main__":
    main()
