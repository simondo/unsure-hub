# SureFlap Cat Flap Connect — Local 802.15.4 / MiWi Protocol Reference

This document summarizes the over-the-air protocol used between a **SureFlap Microchip Cat Flap Connect** (tested; the Pet Door Connect and other register-based SureFlap devices are expected to be similar but untested) and a local coordinator emulated on an **ESP32-C5**.

---

## 1. Radio & Link Layer (IEEE 802.15.4 / MiWi)

* **Standard:** IEEE 802.15.4 (2.4 GHz O-QPSK)
* **Channel:** `15` (2425 MHz)
* **PAN ID:** `0x3421`
* **Coordinator Short Address:** `0xfffe`
* **Coordinator EUI-64:** Microchip OUI `80:1f:12:ff:fe:xx:xx:xx` (keep constant once paired)
* **Check-in Cadence:** Once associated, the flap wakes every **5.56 seconds** and sends a `0x08` online beacon.

### Pairing & Session Handshake

1. **Beacon Request (`0x07`):** Flap scans on Channel 15 when pairing button is pressed (or when hub is unreachable). Coordinator responds with a 7-byte beacon payload `{ 0xff, 0x45, 0x00, 0x00, 0x7e, 0x01|0x02, 0x00 }`.
2. **Association (`0x01`, MAC command frame):** Coordinator responds with `{ 0x02, 0xfe, 0xff, 0x00 }`.
3. **Start (`0x13 <ctr>`):** Coordinator responds with `{ 0x14, <hub_txseq>, <ctr>, 0x00 }`.
4. **Periodic Check-in (`0x08 <ctr> ...`):**
   * Normal (nothing to send): Coordinator replies `0x0a <hub_txseq> <dev_ctr>` (flap returns to sleep).
   * Command pending: Coordinator replies **`0x09 <hub_txseq> <dev_ctr>`** ("stay awake, data pending").
   * Flap stays in RX for ~100–200 ms. Coordinator waits ~15 ms, then sends the `0x01` command frame (MAC ACK requested).
   * Flap acknowledges with a MiWi **`0x02`** frame; coordinator closes the window with `0x0a`.
   * On the next check-in (5.56 s later), the flap echoes back the updated register(s) as `0x84` Report frames.

---

## 2. Application Frame Format & Per-Session XOR Obfuscation

Application payloads start with `0x01`:

```
Byte:   0    1     2    3     4      5      6      7      8      9     10..(L-2)   L-1
      +----+-----+----+----+------+------+------+------+------+------+-----------+-----+
      | 01 | seq | H0 | H1 | len2 |  op  | regH | regL | 0x00 | rlen |  data...  | chk |
      +----+-----+----+----+------+------+------+------+------+------+-----------+-----+
       \____ cleartext ___/ \________________ XORed with session keystream k[4..L-1] ___/
```

* **`0x01`**: Data frame marker.
* **`seq`**: Rolling sequence counter.
* **`H0 H1`**: 2-byte session identifier. Rotates whenever the flap re-pairs (or batteries are pulled), along with the XOR keystream `k[4..]`.
* **`len2`**: `frame_length - 2` (encrypted with `k[4]`).
* **`op`**: Operation code (encrypted with `k[5]`):
  * `0x84` (`132`): Register Report (flap → hub)
  * `0x02` (`2`): Write Register (hub → flap)
* **`regH regL`**: Big-endian 16-bit register address (`k[6]`, `k[7]`).
* **`0x00 rlen`**: Big-endian 16-bit register data length (`k[8]`, `k[9]`).
* **`data...`**: `rlen` bytes of register payload (`k[10..10+rlen-1]`).
* **`chk`**: Single-byte XOR checksum (`k[L-1]`).

### Universal XOR Checksum Invariant

Across all frame lengths `L` and both opcodes (`0x84` report and `0x02` write):

$$\bigoplus_{i=4}^{L-1} \text{plain}[i] = \text{0x2C}$$

Equivalently, for any fixed frame length `L` within a session, the XOR of all ciphertext bytes `raw[4..L-1]` is a constant:

$$\bigoplus_{i=4}^{L-1} \text{raw}[i] = \text{0x2C} \oplus \bigoplus_{i=4}^{L-1} k[i]$$

For 12-byte single-register frames (`rlen = 1`, e.g. `setlock` and `writereg`), once `chk12 = XOR(raw[4..11])` is observed from any 12-byte report, byte 11 of any outbound 12-byte command is simply:

$$\text{raw}[11] = \text{chk12} \oplus \bigoplus_{i=4}^{10} \text{raw}[i]$$

### Auto-Learning a New Session Keystream

Whenever a flap pairs or rotates `H0 H1`, the keystream bytes `k[4..11]` needed for full decoding and 1-byte register writes can be recovered automatically without knowing the hub's master secret:

1. Any `0x84` report gives `k[4] = raw[4] ^ (L - 2)`, `k[5] = raw[5] ^ 0x84`, `k[8] = raw[8] ^ 0x00`.
2. Any 24-byte movement frame (`reg 1024 = 0x0400`, `rlen = 13`) gives `k[6] = raw[6] ^ 0x04`, `k[7] = raw[7] ^ 0x00`, `k[9] = raw[9] ^ 13`, and `k[11], k[12]` from the local `HH:MM` timestamp.
3. Any 12-byte register frame gives `chk12 = XOR(raw[4..11])`, which then solves `k[10] = chk12 ^ 0x2C ^ XOR(k[4..9], k[11])`.

---

## 3. Verified Register Map (Cat Flap Connect)

| Register | Hex | Length | R/W | Description |
|---:|---|---:|---|---|
| `33` | `0x0021` | 3 or 5 | R | Hourly battery ADC (`V = 2.1075 + adc * 0.0225`), followed by clock `HH`, `MM` (and lock mode + reg 37 in 5-byte form) |
| `34` | `0x0022` | 1 | R/W | Internal clock **Hour** (`0..23`) |
| `35` | `0x0023` | 1 | R/W | Internal clock **Minute** (`0..59`) |
| `36` | `0x0024` | 1 | R/W | **Lock Mode**: `0`=Unlocked, `1`=Keep In, `2`=Keep Out, `3`=Locked Both, `4`=Curfew *(physical rotary dial must be in Position 1)* |
| `40` | `0x0028` | 1 | R | Lock-out / pin status reported after lock state changes |
| `321` | `0x0141` | 6 | R | Stored default/backup curfew block |
| `519` | `0x0207` | 6 | R/W | **Curfew block**: `[state, lock_hr, lock_min, unlock_hr, unlock_min, flag]` (`state`: `1`=off, `2`=on, `3`=paused/overridden) |
| `520` | `0x0208` | 1 | R/W | Curfew **Lock Hour** (`0..23`) |
| `521` | `0x0209` | 1 | R/W | Curfew **Lock Minute** (`0..59`) |
| `522` | `0x020a` | 1 | R/W | Curfew **Unlock Hour** (`0..23`) |
| `523` | `0x020b` | 1 | R/W | Curfew **Unlock Minute** (`0..59`) |
| `528 + 3*(n-1)` | `0x0210+` | 3 | R | Pet slot `n` (`1, 2, ...`) movement follow-up (`HH`, `MM`, direction) |
| `621` | `0x026d` | 9 | R | Unknown / unchipped pet exit (`HH`, `MM`, ...) |
| `634` | `0x027a` | 16 | R | Hourly curfew telemetry (`byte 10 == 0x02` → open, otherwise locked) |
| `1024` | `0x0400` | 13 | R | Movement trigger (24-byte frame with `HH`, `MM`, direction nibble `1`=in, `2`=out) |

> **Tip:** Writing individual 1-byte registers (`520`, `521`, `522`, `523`, `519`) only requires keystream bytes `k[4..11]` and `chk12` — and after each write, the flap echoes back the full 6-byte `reg 519` block so you can verify every byte live.
