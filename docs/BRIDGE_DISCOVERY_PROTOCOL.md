# Dongle discovery firmware 1.0.0 — USB protocol revision 1

Built 2026-09-16 for PCA10059 / nrf52840dongle/nrf52840, NCS 3.4.0.
Artifact: `app/firmware/dongle_bridge.hex`. Build provenance/checksum:
`app/firmware/dongle_bridge.manifest.json`. Previous image is preserved in
`app/firmware/backups/dongle_bridge_before_discovery_2026-09-16.hex`.

## User installation and validation

This release changes the **dongle**, not the headstage. No Mac Bluetooth is used.
The current 2.1.0 Device Setup and Studio use dongle discovery. Follow the
[current step-by-step manual](USER_MANUAL_KO.md) for installation and operation.
Legacy SET_TARGET/raw mode is retained for compatibility; the old Pairer is not
part of the current deployment. The checks below describe firmware-build validation.

1. Stop recordings and release dongles in Device Setup, Studio and Builder.
2. Press the dongle's physical RESET button and select Open DFU Bootloader in
   nRF Connect Programmer (red breathing LED).
3. Clear files, add `app/firmware/dongle_bridge.hex`, then Write.
4. After re-enumeration, the neutral image is idle; it does not automatically pair.
5. To test discovery from the project root, with pyserial installed:

   ```bash
   python3 tools/bridge_discover.py --port /dev/cu.usbmodem1101
   ```

   Replace the port with that dongle's current port. The diagnostic clears the
   selected dongle's existing target, scans for 8 seconds, prints candidates, and
   never connects. Do not run it against a recording dongle. Successful completion
   returns the dongle to raw mode. On interruption/error, replug before using an
   old app. BLE-connected headstages may not advertise; absence is not proof of power off.

Hardware flashing, RF discovery, throughput and long-duration recording remain to
be tested. The build, native module tests, frame decoder tests and HEX structural
checks passed. The SDK emits existing legacy-USB deprecation warnings.

## Compatibility and negotiation

Downstream command prefix remains `CB 1D 70 2A`; fields are little-endian.
Legacy `01 + target:u32` keeps the old exact-name target selection, restores raw
notification mode and cancels discovery. Target 0 stops targeting; FFFFFFFF keeps
legacy ANY behavior for old clients. The experiment GUI must never use ANY.
The neutral distribution now ignores historical settings/UICR target values at
boot. Explicit factory-stamped or compile-time target images remain possible.

New clients first send legacy target 0, wait for asynchronous disconnect, then
request capabilities `10`. Retry at 500 ms up to 5 seconds. CAPABILITIES is only
accepted when idle; a busy raw-mode dongle does not reply. Unsupported firmware
also does not reply. Do not infer firmware age from a timeout alone: port ownership,
USB connection and radio initialization also matter. After capabilities arrive,
**all** subsequent upstream DATA and control are enveloped until legacy SET_TARGET
or power-cycle. Do not issue SET_TARGET while expecting envelope mode; use CONNECT
and DISCONNECT instead. The app needs one reader per serial port.

## Commands

| Opcode | Payload after opcode | Meaning |
|---|---|---|
| 01 | target:u32 | Legacy target; switches to raw mode |
| 02 | none | Status; legacy layout in raw mode, 82 envelope in framed mode |
| 10 | none | Capabilities/enter envelope mode while idle |
| 11 | duration_ms:u16, search_id:u32 | Discovery only, 100–30000 ms, default host choice 8000 |
| 12 | none | Stop current discovery and report collected results |
| 13 | none | Replay completed candidate table while idle |
| 14 | token:u32, expected_id:u32 | Connect a discovered candidate |
| 15 | none | Cancel search/target/reconnect and request disconnect |

Commands are queued (8 slots); BLE work runs on the system workqueue. A gap of
500 ms in an incomplete command discards that partial command. Host writes must
send a complete command promptly and serialize requests. Downstream commands retain
the old fixed-size format; they do not have a CRC. Unknown commands receive an
unsupported ACK only after negotiation. Normal headstage control bytes retain
legacy passthrough behavior when they do not match the bridge prefix.

SCAN is rejected if scanning, connecting or connected; it never steals a link.
Only connectable primary advertisements with canonical complete names
`CBRAIN_<1..4294967294>` are candidates. No leading zeros or wildcard IDs.
Names are reconstructed from the reported ID. The existing headstage firmware
puts its complete name in the primary advertisement; scan-response-only names are
not supported in this release.

Results are delivered at completion/explicit stop, not continuously. Up to 32
addresses are retained, deduplicated by address/type. RSSI and age use the latest
observation. Table overflow is explicitly reported. Token uniqueness lasts for the
current power cycle; treat reconnect/power-cycle as a new generation. Scan clears
the old table. Tokens expire 10 seconds after their latest advertisement; scan
again when stale. CONNECT rejects different addresses with the same ID in the
current table. After acceptance it pins both address/type and exact advertised
name. DATA device_id must additionally be verified in the host before recording.

Connection setup has a 30-second deadline. After disconnect, only that same
selected address/name is retried with a new 30-second deadline. Failed attempts
do not extend an active deadline. DISCONNECT clears the target, so it does not
reconnect. ACK means command accepted, not that GATT/data is ready.

## Upstream envelope

`CB 1D 70 2B | type:u8 | length:u16 | payload:length | crc:u16`

Maximum payload 256. CRC16-CCITT-FALSE (poly 1021, init FFFF) covers type, length
and payload, excluding magic and CRC. BLE fragments are each enveloped separately;
concatenate type 90 payloads into the existing CB data decoder. Never search for
control magic inside a validated data payload. Each envelope is queued atomically
or wholly dropped; total dropped bytes are reported. One reader demultiplexes all
frames; separate readers for commands and DATA are prohibited.

| Type | Payload |
|---|---|
| 80 ACK | command:u8, status:u8, detail:u32 |
| 82 STATUS | target:u32, flags:u8, scan_error:i8, adv_seen:u16, match_seen:u16, attempts:u8, connect_error:i8 |
| 90 DATA | Original BLE notification fragment, unchanged |
| 91 CAPS | protocol:u8=1, major:u8=1, minor:u8=0, patch:u8=0, capacity:u8=32, features:u8=0F |
| 92 RESULT | search_id:u32, token:u32, device_id:u32, addr_type:u8, addr_bytes:6, rssi:i8, age_ms:u32 |
| 93 DONE | search_id:u32, reason:u8, count:u8, overflow:u8, tx_dropped:u32 |
| 94 LINK | stage:u8, reason:u8, target:u32 |

CAPS features: bit0 discovery, bit1 candidate connect, bit2 disconnect, bit3 envelopes.
STATUS flags: bit0 connection object exists (includes connecting), bit1 control
handle found, bit2 discovery active, bit3 address-pinned target.
ACK status: 0 accepted, 1 busy/not ready, 2 invalid/duplicate ID, 3 stale/unknown
candidate, 4 unsupported, 5 BLE error, 6 queue full. Detail is command-specific
(search ID, candidate token, conflicting ID, or signed error represented as u32).
DONE reasons: 0 timer completed, 1 explicitly stopped, 2 replay.
LINK stages: 0 disconnected/attempt failed, 1 BLE connected, 2 GATT control found.
Stage 2 is not proof of valid streaming. First valid matching DATA is the host's
readiness condition. Timeout sends ACK(CONNECT,5,ETIMEDOUT).

No state change may be inferred solely from an LED or successful serial write.
The green LED blinks during discovery/target search, stays on when the control
handle is found, and is off while idle.

## Reproduce checks

```bash
cc -std=c11 -Wall -Wextra -Werror -fsanitize=address,undefined \
  firmware/cb_bridge/tests/discovery_test.c -o /tmp/cbrain-discovery-test
/tmp/cbrain-discovery-test
python3 -m unittest discover -s tests -p test_bridge_discovery.py -v
bash tools/build_bridge_discovery.sh
```

Build output: `build/bridge_discovery/zephyr/zephyr.hex`. The script builds only;
it does not publish over the distribution HEX or flash hardware. If relocating
again, use a fresh build directory/pristine configure. Final HEX is constrained to
application flash starting at 0x1000 and below 0xF0000; no bootloader or UICR writes
are in the distributed image.
