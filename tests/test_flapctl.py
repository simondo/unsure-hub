import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bridge"))
import flapctl  # noqa: E402


class TestFlapCtl(unittest.TestCase):
    def test_parse_hhmm_valid(self):
        self.assertEqual(flapctl.parse_hhmm("00:00"), (0, 0))
        self.assertEqual(flapctl.parse_hhmm("18:30"), (18, 30))
        self.assertEqual(flapctl.parse_hhmm("23:59"), (23, 59))

    def test_parse_hhmm_invalid(self):
        for bad in ("1830", "24:00", "12:60", "-1:00", "12:-5", "aa:bb", "1:2:3"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    flapctl.parse_hhmm(bad)

    def _run_cli(self, argv, env=None):
        sent = []
        with patch.object(sys, "argv", ["flapctl.py"] + argv), \
             patch.dict(os.environ, env or {}, clear=True), \
             patch("flapctl.send_udp", side_effect=lambda ip, port, cmd: sent.append((ip, port, cmd))), \
             patch("flapctl.time.sleep"):
            flapctl.main()
        return sent

    def test_basic_commands_and_env_ip(self):
        self.assertEqual(
            self._run_cli(["-c", "/nonexistent", "lock", "keepin"], env={"ESP32_IP": "192.0.2.9"}),
            [("192.0.2.9", 17755, "setlock keepin")],
        )
        self.assertEqual(
            self._run_cli(["-c", "/nonexistent", "--ip", "192.0.2.8", "sink", "192.0.2.1"]),
            [("192.0.2.8", 17755, "sink 192.0.2.1")],
        )
        self.assertEqual(
            self._run_cli(["-c", "/nonexistent", "--ip", "192.0.2.8", "factoryreset"]),
            [("192.0.2.8", 17755, "factoryreset")],
        )
        self.assertEqual(
            self._run_cli(["-c", "/nonexistent", "--ip", "192.0.2.8", "writereg", "520", "0x12"]),
            [("192.0.2.8", 17755, "writereg 520 18")],
        )

    def test_curfew_enable_sends_all_six_registers_in_order(self):
        sent = self._run_cli(["-c", "/nonexistent", "--ip", "192.0.2.8", "curfew", "18:30", "06:00", "--enable", "--delay", "0"])
        self.assertEqual(
            [cmd for _, _, cmd in sent],
            [
                "writereg 520 18",
                "writereg 521 30",
                "writereg 522 6",
                "writereg 523 0",
                "writereg 519 2",
                "setlock curfew",
            ],
        )

    def test_missing_ip_exits(self):
        with self.assertRaises(SystemExit):
            self._run_cli(["-c", "/nonexistent", "getkey"])


if __name__ == "__main__":
    unittest.main()
