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

        self.pets: Dict[int, str] = {int(k): v for k, v in cfg.get("pets", {}).items()}
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
        self.ha_post("/api/states/sensor.catflap_curfew", {
            "state": "locked" if locked else "open",
            "attributes": {
                "friendly_name": "Cat flap curfew",
                "icon": "mdi:lock-clock" if locked else "mdi:lock-open-variant",
                "lock_time": lo,
                "unlock_time": hi,
                "flap_curfew_state": c_info.get("state", "unknown"),
                "detail": "Keep in: pets can re-enter but not exit" if locked else "Pets can come and go",
            },
        })
        self.push_lock_sensor()
        self.push_battery_sensor()
        ev = self.state.get("last_event")
        if ev:
            self.ha_post("/api/states/sensor.catflap_last_event", {
                "state": self._fmt_event(ev["pet"], ev["direction"]),
                "attributes": {**ev, "friendly_name": "Cat flap last event", "icon": "mdi:door"},
            })
        for pet, loc in self.state.get("location", {}).items():
            self.ha_post(f"/api/states/sensor.catflap_{pet.lower()}", {
                "state": loc,
                "attributes": {
                    "since": self.state.get("since", {}).get(pet),
                    "friendly_name": pet,
                    "icon": "mdi:cat" if loc == "inside" else "mdi:tree",
                },
            })

    def push_lock_sensor(self) -> None:
        self.ha_post("/api/states/sensor.catflap_lock_mode", {
            "state": self.state.get("lock_mode", "Unknown"),
            "attributes": {
                "friendly_name": "Cat flap lock mode",
                "icon": "mdi:door-closed-lock",
                "command_status": self.cmd_status,
                "curfew_schedule": self.state.get("curfew"),
            },
        })

    def push_battery_sensor(self) -> None:
        batt = self.state.get("battery")
        if not batt:
            return
        self.ha_post("/api/states/sensor.catflap_battery", {
            "state": batt["volts"],
            "attributes": {
                "friendly_name": "Cat flap battery",
                "unit_of_measurement": "V",
                "device_class": "voltage",
                "state_class": "measurement",
                "icon": "mdi:battery" if batt["pct"] > 25 else "mdi:battery-alert",
                "battery_pct": batt["pct"],
                "adc": batt["adc"],
                "flap_clock": batt.get("clock"),
                "updated": batt.get("time"),
            },
        })

    def sync_ha_select(self, mode_name: str) -> None:
        self.last_ha_lock = mode_name
        self.ha_post("/api/services/input_select/select_option", {
            "entity_id": self.ha_select,
            "option": mode_name,
        })

    def poll_ha_lock(self) -> None:
        st = self.ha_get(f"/api/states/{self.ha_select}")
        if not st or "state" not in st:
            return
        val = st["state"]
        if self.last_ha_lock is None:
            self.last_ha_lock = val
            return
        if val != self.last_ha_lock:
            self.last_ha_lock = val
            code = LOCK_BY_NAME.get(val.lower())
            if code is not None:
                self.cmd_status = f"sending {val}"
                self.send_esp(f"setlock {code}")
                self.push_lock_sensor()

    # ---- Protocol decoding & keystream auto-learning ----

    def _fmt_event(self, pet: str, direction: str) -> str:
        if pet == "unknown":
            return {"out": "A pet went out", "in": "A pet came in",
                    "tried to go out": "A pet tried to go out"}.get(direction, f"A pet {direction}")
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
        if pet != "unknown" and direction in ("in", "out"):
            loc[pet] = "outside" if direction == "out" else "inside"
            since[pet] = t
        ev = {"pet": pet, "direction": direction, "flap_time": flap_time, "inferred": inferred, "time": t}
        self.state["last_event"] = ev
        self._save_state()
        ev_str = self._fmt_event(pet, direction)
        self.log(f"EVENT: {ev_str} (flap {flap_time}{', inferred' if inferred else ''})")
        self.ha_post("/api/events/catflap_event", ev)
        self.ha_post("/api/states/sensor.catflap_last_event", {
            "state": ev_str,
            "attributes": {**ev, "friendly_name": "Cat flap last event", "icon": "mdi:door"},
        })
        if pet != "unknown":
            self.ha_post(f"/api/states/sensor.catflap_{pet.lower()}", {
                "state": loc.get(pet, "unknown"),
                "attributes": {
                    "since": t,
                    "friendly_name": pet,
                    "inferred": inferred,
                    "icon": "mdi:cat" if loc.get(pet) == "inside" else "mdi:tree",
                },
            })

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
            mn = (b[12] ^ k[12]) if 12 in k else 0
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
            dir_byte = self.last24_dir if self.last24_dir is not None else ((b[12] ^ k[12]) if 12 in k else 0x61)
            direction = "in" if (dir_byte & 0x0F) == 1 else "out"
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


def main():
    ap = argparse.ArgumentParser(description="SureFlap ESP32-C5 Local Bridge")
    ap.add_argument("-c", "--config", default=os.environ.get("SUREFLAP_CONFIG", "config.json"))
    args = ap.parse_args()
    with open(args.config) as f:
        cfg = json.load(f)
    SureFlapBridge(cfg).run()


if __name__ == "__main__":
    main()
