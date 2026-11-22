#!/usr/bin/env python3
"""CLI utility for controlling a SureFlap Pet Door Connect via the ESP32-C5 local hub.

Examples:
  ./flapctl.py lock keepin
  ./flapctl.py lock curfew
  ./flapctl.py curfew 18:30 06:00 --enable
  ./flapctl.py synctime
  ./flapctl.py writereg 520 18
"""

import argparse
import datetime
import json
import os
import socket
import sys
import time


def send_udp(ip: str, port: int, cmd: str) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(cmd.encode(), (ip, port))
    print(f"-> {ip}:{port}  {cmd}")


def parse_hhmm(s: str):
    parts = s.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Expected HH:MM, got {s!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Invalid time {s!r}")
    return h, m


def main():
    ap = argparse.ArgumentParser(description="Control SureFlap Pet Door via ESP32-C5 hub")
    ap.add_argument("-c", "--config", default=os.environ.get("SUREFLAP_CONFIG", "config.json"))
    ap.add_argument("--ip", help="Override ESP32 IP address")
    ap.add_argument("--port", type=int, help="Override ESP32 UDP control port (default 17755)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_lock = sub.add_parser("lock", help="Set lock mode")
    p_lock.add_argument("mode", choices=["unlocked", "keepin", "keepout", "locked", "curfew", "0", "1", "2", "3", "4"])

    p_curf = sub.add_parser("curfew", help="Program onboard curfew schedule (HH:MM HH:MM)")
    p_curf.add_argument("lock_time", help="Time to lock pets in (HH:MM, 24h, e.g. 18:30)")
    p_curf.add_argument("unlock_time", help="Time pets may exit (HH:MM, 24h, e.g. 06:00)")
    p_curf.add_argument("--enable", action="store_true", help="Also switch flap into Curfew mode (reg 36 = 4)")
    p_curf.add_argument("--delay", type=float, default=7.0, help="Seconds between register writes (default 7s)")

    p_time = sub.add_parser("synctime", help="Sync flap internal clock (reg 34/35) to local host time")
    p_time.add_argument("--delay", type=float, default=7.0)

    p_wr = sub.add_parser("writereg", help="Write a single 1-byte Pet Door register")
    p_wr.add_argument("reg", type=int)
    p_wr.add_argument("val", type=lambda x: int(x, 0))

    sub.add_parser("getkey", help="Ask ESP32 to report its current session keystream")

    p_sink = sub.add_parser("sink", help="Change ESP32 bridge server IP at runtime (saved to NVS)")
    p_sink.add_argument("sink_ip", help="New bridge server IPv4 address")

    sub.add_parser("factoryreset", help="Erase ESP32 NVS config and reboot into SureFlap-Hub-Setup AP")

    args = ap.parse_args()
    cfg = {}
    if os.path.isfile(args.config):
        with open(args.config) as f:
            cfg = json.load(f)
    ip = args.ip or os.environ.get("ESP32_IP") or cfg.get("esp32_ip")
    port = args.port or int(cfg.get("esp32_ctrl_port", 17755))
    if not ip:
        sys.exit("Error: provide --ip, set ESP32_IP env var, or set esp32_ip in config.json")

    if args.cmd == "lock":
        send_udp(ip, port, f"setlock {args.mode}")
    elif args.cmd == "getkey":
        send_udp(ip, port, "getkey")
    elif args.cmd == "sink":
        send_udp(ip, port, f"sink {args.sink_ip}")
    elif args.cmd == "factoryreset":
        send_udp(ip, port, "factoryreset")
    elif args.cmd == "writereg":
        send_udp(ip, port, f"writereg {args.reg} {args.val}")
    elif args.cmd == "synctime":
        now = datetime.datetime.now()
        send_udp(ip, port, f"writereg 34 {now.hour}")
        time.sleep(args.delay)
        send_udp(ip, port, f"writereg 35 {now.minute}")
        print(f"Synced flap clock to {now.strftime('%H:%M')}.")
    elif args.cmd == "curfew":
        lh, lm = parse_hhmm(args.lock_time)
        uh, um = parse_hhmm(args.unlock_time)
        steps = [
            (f"writereg 520 {lh}", f"From-Hour = {lh:02d}"),
            (f"writereg 521 {lm}", f"From-Min  = {lm:02d}"),
            (f"writereg 522 {uh}", f"To-Hour   = {uh:02d}"),
            (f"writereg 523 {um}", f"To-Min    = {um:02d}"),
        ]
        if args.enable:
            steps.append(("writereg 519 2", "CurfewState = ON (2)"))
            steps.append(("setlock curfew", "LockMode = Curfew (4)"))
        for i, (c, desc) in enumerate(steps):
            print(f"[{i+1}/{len(steps)}] {desc}")
            send_udp(ip, port, c)
            if i < len(steps) - 1:
                time.sleep(args.delay)
        print(f"Done. Curfew programmed to {lh:02d}:{lm:02d} -> {uh:02d}:{um:02d}.")


if __name__ == "__main__":
    main()
