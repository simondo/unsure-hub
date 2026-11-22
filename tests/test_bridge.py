import http.client
import json
import os
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bridge"))
import sureflap_bridge as sb  # noqa: E402


# Deterministic 27-byte session keystream for unit tests
TEST_KEY = {i: (0x37 + i * 13) & 0xFF for i in range(4, 27)}
TEST_HDR = "a1b2"


def encrypt_frame(hdr_hex: str, plain: bytes, key: dict) -> bytes:
    """Build a raw encrypted SureFlap frame from plaintext bytes[4:]."""
    assert len(plain) >= 12 and plain[0] == 0x01
    out = bytearray(plain)
    out[2:4] = bytes.fromhex(hdr_hex)
    for i in range(4, len(plain)):
        out[i] = plain[i] ^ key[i]
    return bytes(out)


def stat_line(raw: bytes) -> str:
    return f"HUB rssi=-65 len={len(raw)} data {raw.hex()}"


def make_12b_reg36(mode_code: int) -> bytes:
    """Build a plaintext 12B reg 36 (lock mode) frame satisfying XOR(plain[4..11]) == 0x2c."""
    # bytes: 0..3 header, 4=len-2(10), 5=0x84, 6..7=reg(36), 8=0x00, 9=rlen(1), 10=val, 11=checksum
    b = bytearray([0x01, 0x00, 0x00, 0x00, 10, 0x84, 0x00, 36, 0x00, 1, mode_code, 0x00])
    xr = 0
    for v in b[4:11]:
        xr ^= v
    b[11] = xr ^ 0x2C
    return bytes(b)


def make_24b_movement(hour: int, minute: int, dir_byte: int) -> bytes:
    """Build a plaintext 24B movement trigger frame (reg 1024, rlen 13)."""
    b = bytearray(24)
    b[0] = 0x01
    b[4] = 22
    b[5] = 0x84
    b[6], b[7] = 0x04, 0x00  # reg 1024
    b[8] = 0x00
    b[9] = 13
    b[11] = hour
    b[12] = minute
    b[13] = dir_byte
    return bytes(b)


def make_14b_pet(slot: int, dir_byte: int = 0x61) -> bytes:
    """Build a plaintext 14B named pet follow-up frame (reg 528 + 3*(slot-1), rlen 3)."""
    reg = 528 + 3 * (slot - 1)
    b = bytearray(14)
    b[0] = 0x01
    b[4] = 12
    b[5] = 0x84
    b[6], b[7] = (reg >> 8) & 0xFF, reg & 0xFF
    b[9] = 3
    b[12] = dir_byte
    return bytes(b)


def make_14b_battery(adc: int, hr: int, mn_raw: int) -> bytes:
    """Build a plaintext 14B reg 33 battery + clock frame (rlen 3)."""
    b = bytearray(14)
    b[0] = 0x01
    b[4] = 12
    b[5] = 0x84
    b[6], b[7] = 0x00, 33
    b[9] = 3
    b[10] = adc
    b[11] = hr
    b[12] = mn_raw
    return bytes(b)


def make_16b_battery_with_lock(adc: int, hr: int, mn_raw: int, lock_code: int) -> bytes:
    """Build a plaintext 16B reg 33 battery + clock + lock frame (rlen 5)."""
    b = bytearray(16)
    b[0] = 0x01
    b[4] = 14
    b[5] = 0x84
    b[6], b[7] = 0x00, 33
    b[9] = 5
    b[10] = adc
    b[11] = hr
    b[12] = mn_raw
    b[13] = lock_code
    return bytes(b)


def make_17b_curfew(state_code: int, fh: int, fm: int, th: int, tm: int) -> bytes:
    """Build a plaintext 17B reg 519 curfew schedule frame (rlen 6)."""
    b = bytearray(17)
    b[0] = 0x01
    b[4] = 15
    b[5] = 0x84
    b[6], b[7] = (519 >> 8) & 0xFF, 519 & 0xFF
    b[9] = 6
    b[10] = state_code
    b[11], b[12], b[13], b[14] = fh, fm, th, tm
    return bytes(b)


def make_27b_curfew_telemetry(val: int) -> bytes:
    """Build a plaintext 27B reg 634 hourly curfew telemetry frame (rlen 16)."""
    b = bytearray(27)
    b[0] = 0x01
    b[4] = 25
    b[5] = 0x84
    b[6], b[7] = (634 >> 8) & 0xFF, 634 & 0xFF
    b[9] = 16
    b[10] = val
    return bytes(b)


def make_20b_unknown_exit(hr: int, mn: int) -> bytes:
    """Build a plaintext 20B reg 621 unknown pet exit frame (rlen 9)."""
    b = bytearray(20)
    b[0] = 0x01
    b[4] = 18
    b[5] = 0x84
    b[6], b[7] = (621 >> 8) & 0xFF, 621 & 0xFF
    b[9] = 9
    b[10], b[11] = hr, mn
    return bytes(b)


class BridgeTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmpdir.name, "state.json")

    def tearDown(self):
        self.tmpdir.cleanup()

    def make_bridge(self, *, preload_key=True, toggle_on_in=False, pets=None, extra_cfg=None):
        cfg = {
            "esp32_ip": "192.0.2.10",
            "esp32_ctrl_port": 17755,
            "state_file": self.state_file,
            "pets": pets if pets is not None else {"1": "Luna", "2": "Milo"},
            "toggle_on_in": toggle_on_in,
            "curfew_fallback": {"lock_time": "18:30", "unlock_time": "06:00"},
        }
        if extra_cfg:
            cfg.update(extra_cfg)
        bridge = sb.SureFlapBridge(cfg)
        self.addCleanup(bridge.udp_tx.close)
        bridge.sent_udp = []
        bridge.send_esp = lambda cmd: bridge.sent_udp.append(cmd)
        bridge.ha_post = MagicMock(return_value=b"{}")
        bridge.ha_get = MagicMock(return_value=None)
        if preload_key:
            bridge.sessions[TEST_HDR] = dict(TEST_KEY)
            bridge.active_hdr = TEST_HDR
        return bridge


class TestKeystreamAutoLearning(BridgeTestBase):
    def test_learns_full_keystream_and_pushes_setkey(self):
        bridge = self.make_bridge(preload_key=False)

        # 1) 24B movement trigger at 14:25 teaches k[4..9], k[11], k[12]
        raw24 = encrypt_frame(TEST_HDR, make_24b_movement(14, 25, 0x61), TEST_KEY)
        with patch("sureflap_bridge.time.strftime", return_value="14:25:00"):
            bridge.handle_stat_line(stat_line(raw24))

        learned = bridge.sessions[TEST_HDR]
        for idx in (4, 5, 6, 7, 8, 9, 11, 12):
            self.assertEqual(learned[idx], TEST_KEY[idx], f"mismatch at k[{idx}]")
        self.assertNotIn(10, learned)
        self.assertEqual(bridge.sent_udp, [])  # not ready until k[10] + chk12 are solved

        # 2) 12B reg 36 frame teaches chk12 and solves k[10] via XOR(plain[4..11]) == 0x2c
        raw12 = encrypt_frame(TEST_HDR, make_12b_reg36(1), TEST_KEY)
        with patch("sureflap_bridge.time.strftime", return_value="14:25:10"):
            bridge.handle_stat_line(stat_line(raw12))

        self.assertEqual(bridge.sessions[TEST_HDR][10], TEST_KEY[10])
        self.assertEqual(bridge.state["lock_mode"], "Keep In")
        self.assertEqual(bridge.cmd_status, "confirmed")

        expected_khex = bytes(TEST_KEY[i] for i in range(4, 12)).hex()
        expected_chk12 = 0
        for v in raw12[4:]:
            expected_chk12 ^= v
        self.assertEqual(bridge.sent_udp, [f"setkey {TEST_HDR} {expected_khex} {expected_chk12:02x}"])

        # Verify state persisted to disk
        with open(self.state_file) as f:
            saved = json.load(f)
        self.assertEqual(saved["active_hdr"], TEST_HDR)
        self.assertEqual(saved["sessions"][TEST_HDR]["10"], TEST_KEY[10])


class TestProtocolDecoding(BridgeTestBase):
    def test_reg36_lock_mode_and_5s_dedup(self):
        bridge = self.make_bridge()
        raw = encrypt_frame(TEST_HDR, make_12b_reg36(0), TEST_KEY)

        with patch("sureflap_bridge.time.time", return_value=1000.0):
            bridge.handle_stat_line(stat_line(raw))
        self.assertEqual(bridge.state["lock_mode"], "Unlocked")
        first_calls = bridge.ha_post.call_count

        # Duplicate within 5s must be ignored
        with patch("sureflap_bridge.time.time", return_value=1003.0):
            bridge.handle_stat_line(stat_line(raw))
        self.assertEqual(bridge.ha_post.call_count, first_calls)

        # Same body after >5s is accepted again
        with patch("sureflap_bridge.time.time", return_value=1006.0):
            bridge.handle_stat_line(stat_line(raw))
        self.assertGreater(bridge.ha_post.call_count, first_calls)

    def test_reg33_battery_and_clock_high_bit_masking(self):
        bridge = self.make_bridge()
        # adc=160 -> 2.1075 + 160*0.0225 = 5.71V, pct = round((5.71-4.4)/1.6*100) = 82%
        # minute byte has high bit set (0x80 | 45 = 0xAD) -> must mask to 45
        raw = encrypt_frame(TEST_HDR, make_14b_battery(160, 19, 0x80 | 45), TEST_KEY)
        bridge.handle_stat_line(stat_line(raw))

        batt = bridge.state["battery"]
        self.assertEqual(batt["volts"], 5.71)
        self.assertEqual(batt["pct"], 82)
        self.assertEqual(batt["adc"], 160)
        self.assertEqual(batt["clock"], "19:45")

    def test_reg33_16b_updates_lock_mode_too(self):
        bridge = self.make_bridge()
        raw = encrypt_frame(TEST_HDR, make_16b_battery_with_lock(140, 8, 5, 3), TEST_KEY)
        bridge.handle_stat_line(stat_line(raw))

        self.assertEqual(bridge.state["lock_mode"], "Locked")
        self.assertEqual(bridge.state["battery"]["clock"], "08:05")

    def test_reg519_curfew_schedule_and_reg634_telemetry(self):
        bridge = self.make_bridge()
        raw519 = encrypt_frame(TEST_HDR, make_17b_curfew(2, 19, 0, 6, 30), TEST_KEY)
        bridge.handle_stat_line(stat_line(raw519))

        self.assertEqual(bridge.state["curfew"], {
            "state_code": 2,
            "state": "on",
            "lock_time": "19:00",
            "unlock_time": "06:30",
        })

        # reg 634: 0x02 => open, anything else => locked
        raw634_open = encrypt_frame(TEST_HDR, make_27b_curfew_telemetry(0x02), TEST_KEY)
        bridge.handle_stat_line(stat_line(raw634_open))
        self.assertEqual(bridge.state["flap_curfew"], "open")
        self.assertFalse(bridge._in_curfew_now("22:00"))

        raw634_locked = encrypt_frame(TEST_HDR, make_27b_curfew_telemetry(0x01), TEST_KEY)
        bridge.handle_stat_line(stat_line(raw634_locked))
        self.assertEqual(bridge.state["flap_curfew"], "locked")
        self.assertTrue(bridge._in_curfew_now("12:00"))

    def test_txcmd_status_lines(self):
        bridge = self.make_bridge()
        bridge.handle_stat_line("txcmd setlock 1")
        self.assertEqual(bridge.cmd_status, "transmitted")
        bridge.handle_stat_line("txcmd-ack ok")
        self.assertEqual(bridge.cmd_status, "acked")


class TestPetStateMachine(BridgeTestBase):
    def _send_pet_event(self, bridge, t_str, slot, dir_byte, now_ts):
        raw24 = encrypt_frame(TEST_HDR, make_24b_movement(int(t_str[:2]), int(t_str[3:5]), dir_byte), TEST_KEY)
        raw14 = encrypt_frame(TEST_HDR, make_14b_pet(slot, dir_byte), TEST_KEY)
        with patch("sureflap_bridge.time.strftime", return_value=t_str), \
             patch("sureflap_bridge.time.time", return_value=now_ts):
            bridge.handle_stat_line(stat_line(raw24))
            bridge.handle_stat_line(stat_line(raw14))

    def test_standard_in_out_and_looked_in(self):
        bridge = self.make_bridge(toggle_on_in=False)
        bridge.state["lock_mode"] = "Unlocked"

        self._send_pet_event(bridge, "10:00:00", 1, 0x61, 100.0)
        self.assertEqual(bridge.state["location"]["Luna"], "inside")
        self.assertEqual(bridge.state["last_event"]["direction"], "in")

        # 0x40 = looked in; must NOT change location
        self._send_pet_event(bridge, "10:01:00", 1, 0x40, 200.0)
        self.assertEqual(bridge.state["location"]["Luna"], "inside")
        self.assertEqual(bridge.state["last_event"]["direction"], "looked in")

        # 0x62 = explicit named exit
        self._send_pet_event(bridge, "10:02:00", 1, 0x62, 300.0)
        self.assertEqual(bridge.state["location"]["Luna"], "outside")
        self.assertEqual(bridge.state["last_event"]["direction"], "out")

    def test_toggle_on_in_unlocked_vs_curfew_locked(self):
        bridge = self.make_bridge(toggle_on_in=True)
        bridge.state["lock_mode"] = "Unlocked"
        bridge.state["location"] = {"Luna": "inside", "Milo": "inside"}

        # Unlocked: 0x61 flips inside -> outside -> inside
        self._send_pet_event(bridge, "12:00:00", 1, 0x61, 100.0)
        self.assertEqual(bridge.state["location"]["Luna"], "outside")
        self.assertEqual(bridge.state["last_event"]["direction"], "out")

        self._send_pet_event(bridge, "12:10:00", 1, 0x61, 200.0)
        self.assertEqual(bridge.state["location"]["Luna"], "inside")
        self.assertEqual(bridge.state["last_event"]["direction"], "in")

        # During Curfew (20:00 is within 18:30 -> 06:00): 0x61 must stay "in", never flip to "outside"
        bridge.state["lock_mode"] = "Curfew"
        self._send_pet_event(bridge, "20:00:00", 1, 0x61, 300.0)
        self.assertEqual(bridge.state["location"]["Luna"], "inside")
        self.assertEqual(bridge.state["last_event"]["direction"], "in")

        # Keep In mode at midday: 0x61 must also force "in"
        bridge.state["lock_mode"] = "Keep In"
        self._send_pet_event(bridge, "12:30:00", 1, 0x61, 400.0)
        self.assertEqual(bridge.state["location"]["Luna"], "inside")
        self.assertEqual(bridge.state["last_event"]["direction"], "in")

    def test_unknown_exit_inference_and_pending_unknown_exits(self):
        bridge = self.make_bridge(toggle_on_in=True)
        bridge.state["lock_mode"] = "Unlocked"

        # Case 1: Only 1 pet inside -> reg 621 infers that pet went outside
        bridge.state["location"] = {"Luna": "inside", "Milo": "outside"}
        raw621 = encrypt_frame(TEST_HDR, make_20b_unknown_exit(11, 15), TEST_KEY)
        with patch("sureflap_bridge.time.strftime", return_value="11:15:00"), \
             patch("sureflap_bridge.time.time", return_value=100.0):
            bridge.handle_stat_line(stat_line(raw621))

        self.assertEqual(bridge.state["location"]["Luna"], "outside")
        self.assertEqual(bridge.state["last_event"]["pet"], "Luna")
        self.assertTrue(bridge.state["last_event"]["inferred"])
        self.assertEqual(bridge.pending_unknown_exits, 0)

        # Case 2: Both pets inside -> ambiguous unknown exit increments pending_unknown_exits;
        # next 0x61 consumes it as "in" instead of flipping an inside pet to "outside".
        bridge.state["location"] = {"Luna": "inside", "Milo": "inside"}
        with patch("sureflap_bridge.time.strftime", return_value="11:20:00"), \
             patch("sureflap_bridge.time.time", return_value=200.0):
            bridge.handle_stat_line(stat_line(raw621))
        self.assertEqual(bridge.state["last_event"]["pet"], "unknown")
        self.assertEqual(bridge.pending_unknown_exits, 1)

        self._send_pet_event(bridge, "11:25:00", 1, 0x61, 300.0)
        self.assertEqual(bridge.state["location"]["Luna"], "inside")
        self.assertEqual(bridge.state["last_event"]["direction"], "in")
        self.assertEqual(bridge.pending_unknown_exits, 0)

    def test_curfew_midnight_wrap_and_tried_to_go_out(self):
        bridge = self.make_bridge()
        # Overnight window 18:30 -> 06:00
        self.assertTrue(bridge._in_curfew_now("18:30"))
        self.assertTrue(bridge._in_curfew_now("23:59"))
        self.assertTrue(bridge._in_curfew_now("00:00"))
        self.assertTrue(bridge._in_curfew_now("05:59"))
        self.assertFalse(bridge._in_curfew_now("06:00"))
        self.assertFalse(bridge._in_curfew_now("12:00"))

        # Daytime window 09:00 -> 17:00
        bridge.state["curfew"] = {"lock_time": "09:00", "unlock_time": "17:00"}
        self.assertTrue(bridge._in_curfew_now("09:00"))
        self.assertTrue(bridge._in_curfew_now("16:59"))
        self.assertFalse(bridge._in_curfew_now("17:00"))

        # Explicit exit during curfew becomes "tried to go out" and does not mark outside
        bridge.state["lock_mode"] = "Curfew"
        bridge.state["location"] = {"Luna": "inside"}
        self._send_pet_event(bridge, "10:00:00", 1, 0x62, 100.0)
        self.assertEqual(bridge.state["last_event"]["direction"], "tried to go out")
        self.assertEqual(bridge.state["location"]["Luna"], "inside")

    def test_manual_flip_toggles_location(self):
        bridge = self.make_bridge()
        bridge.state["location"] = {"Luna": "inside"}
        bridge._flip_pet("Luna")
        self.assertEqual(bridge.state["location"]["Luna"], "outside")
        bridge._flip_pet("Luna")
        self.assertEqual(bridge.state["location"]["Luna"], "inside")


class TestMqttAndConfig(BridgeTestBase):
    def test_mqtt_discovery_and_lock_command(self):
        bridge = self.make_bridge()
        published = []
        mock_mqtt = MagicMock()
        mock_mqtt.publish.side_effect = lambda topic, data, qos=1, retain=True: published.append((topic, data, retain))
        bridge.mqtt_client = mock_mqtt

        bridge._publish_mqtt_discovery()
        topics = {t for t, _, _ in published}
        # 5 core entities + 2 per pet (sensor + flip button) = 9 total
        self.assertEqual(len(published), 9)
        self.assertIn("homeassistant/select/sureflap_lock_mode/config", topics)
        self.assertIn("homeassistant/sensor/sureflap_pet_luna/config", topics)
        self.assertIn("homeassistant/button/sureflap_flip_milo/config", topics)

        # Verify discovery JSON structure
        select_payload = json.loads(next(d for t, d, _ in published if t.endswith("select/sureflap_lock_mode/config")))
        self.assertEqual(select_payload["command_topic"], "sureflap/lock_mode/set")
        self.assertEqual(select_payload["options"], list(sb.LOCK_MODES.values()))

        # Inbound HA lock select -> UDP setlock
        bridge._set_lock_from_ha("Keep In")
        self.assertEqual(bridge.sent_udp, ["setlock 1"])
        bridge._set_lock_from_ha("NotAMode")
        self.assertEqual(bridge.sent_udp, ["setlock 1"])

    def test_load_config_pure_env_vars(self):
        env = {
            "ESP32_IP": "192.0.2.55",
            "PETS": "1:Luna, 2:Milo",
            "MQTT_HOST": "127.0.0.1",
            "MQTT_PORT": "1884",
            "TOGGLE_ON_IN": "true",
            "OTA_ENABLED": "1",
            "CURFEW_LOCK": "19:00",
            "CURFEW_UNLOCK": "07:00",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = sb._load_config("/nonexistent/config.json")
        self.assertEqual(cfg["esp32_ip"], "192.0.2.55")
        self.assertEqual(cfg["pets"], {"1": "Luna", "2": "Milo"})
        self.assertTrue(cfg["mqtt"]["enabled"])
        self.assertEqual(cfg["mqtt"]["host"], "127.0.0.1")
        self.assertEqual(cfg["mqtt"]["port"], 1884)
        self.assertTrue(cfg["toggle_on_in"])
        self.assertTrue(cfg["ota_enabled"])
        self.assertEqual(cfg["curfew_fallback"], {"lock_time": "19:00", "unlock_time": "07:00"})

    def test_load_config_missing_esp32_ip_exits(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                sb._load_config("/nonexistent/config.json")


class TestWebFlasherAndOtaServer(BridgeTestBase):
    def test_web_flasher_endpoints_and_ota_post(self):
        fw_dir = os.path.join(self.tmpdir.name, "fw")
        ota_dir = os.path.join(self.tmpdir.name, "ota")
        os.makedirs(fw_dir)
        with open(os.path.join(fw_dir, "ver.txt"), "w") as f:
            f.write("1.2.3-test\n")
        with open(os.path.join(fw_dir, "sureflap_hub_merged.bin"), "wb") as f:
            f.write(b"MERGED_FW_BYTES")
        with open(os.path.join(fw_dir, "sureflap_hub.bin"), "wb") as f:
            f.write(b"APP_OTA_BYTES")

        # Reserve an ephemeral localhost port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        bridge = self.make_bridge(extra_cfg={
            "ota_dir": ota_dir,
            "firmware_dir": fw_dir,
            "ota_port": port,
        })
        bridge._start_ota_server()
        self.addCleanup(bridge.ota_server.server_close)
        self.addCleanup(bridge.ota_server.shutdown)
        time.sleep(0.05)

        def req(method: str, path: str, headers=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                c.request(method, path, headers=headers or {})
                r = c.getresponse()
                return r.status, dict(r.getheaders()), r.read()
            finally:
                c.close()

        # GET / returns Web Flasher UI with version
        status, _, raw_body = req("GET", "/")
        body = raw_body.decode()
        self.assertEqual(status, 200)
        self.assertIn("esp-web-install-button", body)
        self.assertIn("1.2.3-test", body)

        # GET /manifest.json returns valid ESP Web Tools manifest for ESP32-C5
        status, _, raw_body = req("GET", "/manifest.json")
        manifest = json.loads(raw_body.decode())
        self.assertEqual(status, 200)
        self.assertEqual(manifest["version"], "1.2.3-test")
        self.assertEqual(manifest["builds"][0]["chipFamily"], "ESP32-C5")
        self.assertEqual(manifest["builds"][0]["parts"][0]["offset"], 0)

        # GET /firmware/sureflap_hub_merged.bin serves the baked binary
        status, _, raw_body = req("GET", "/firmware/sureflap_hub_merged.bin")
        self.assertEqual(status, 200)
        self.assertEqual(raw_body, b"MERGED_FW_BYTES")

        # GET missing file -> 404
        status, _, _ = req("GET", "/does_not_exist.bin")
        self.assertEqual(status, 404)

        # POST /api/ota triggers UDP ota command and redirects to /
        status, hdrs, _ = req("POST", "/api/ota", headers={"Host": f"127.0.0.1:{port}"})
        self.assertEqual(status, 303)
        self.assertEqual(hdrs.get("Location"), "/")
        self.assertEqual(bridge.sent_udp, [f"ota http://127.0.0.1:{port}/sureflap_hub.bin"])


if __name__ == "__main__":
    unittest.main()
