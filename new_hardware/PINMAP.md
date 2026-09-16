# CBRAIN Headstage — Pin Map (CB V4 / Dr. Woo)

> Auto-derived from the EasyEDA sources in this folder and verified by netlist tracing:
> - Schematic: [`1-Schematic_CB_INTAN.json`](1-Schematic_CB_INTAN.json) (sheets **MCU**, **POWER**)
> - Dock/charger PCB: [`PCB_INTAN_Charger_2026-07-13.json`](PCB_INTAN_Charger_2026-07-13.json)
>
> **Board:** nRF52832 (BLE MCU) + RHD2216 (Intan 16-ch bio-amp) + W25Q128 flash, single **3V3** rail from a MIC23050 buck.
> Nets marked `*` are inferred from the pin's power/ground name (single-rail board), not from a traced label.

---

## 0. Key nets (for debugging)

| Net | Where to find it | Notes |
|---|---|---|
| **3V3** | P1 **pin 2**; flash U3 **pin 8**; RHD VDD1/2/3; nRF VDD (13/36/48) | Buck output (U4 → L2 → 3V3). Rail that must be up to run/flash. |
| **GND** | P1 **pin 6**; U4 pin 2; battery P2 pin 2; RHD GND pins; nRF EP(49)/VSS | Reference. |
| **BAT** | P2 pin 1; SW1 pin 2; U4 **VIN (pin 1)**; P1 pin 4 (CHG) | Li-ion (~3.7–4.2 V) *before* the switch/regulator. |
| **SWDCLK / SWDIO** | P1 **pin 1 / pin 3**; nRF **pin 25 / 26** | Debug/programming. |

**Power path:** `Battery (P2) → SW1 (power switch) → U4 VIN → [MIC23050 buck + L2] → 3V3 → nRF/RHD/flash/LEDs`.
The J-Link **VTref** reads the **3V3/VDD** node — if VTref = 0 V, the buck isn't producing 3V3 (switch/EN or DC-DC), not a wiring gap.

---

## 1. P1 — 2×8 edge connector (external interface & dock)

The headstage's only external connector (`INTAN 1.27MM 2X8P-EDGE`). Mates with the charger/dock.

| Pin | Name | Net | | Pin | Name | Net |
|---:|---|---|---|---:|---|---|
| **1** | CLK | **SWDCLK** | | **2** | 3.3V | **3V3** |
| **3** | DIO | **SWDIO** | | **4** | CHG | BAT |
| 5 | IN7 | IN7 | | **6** | GND | **GND** |
| 7 | IN6 | IN6 | | 8 | NC | — |
| 9 | IN5 | IN5 | | 10 | REF | REF |
| 11 | NC | — | | 12 | IN0 | IN0 |
| 13 | IN4 | IN4 | | 14 | IN1 | IN1 |
| 15 | IN3 | IN3 | | 16 | IN2 | IN2 |


밧데리 방향 가장 오른쪽 핀이 2(3.3v) 오른쪽에서 세번째가 GND
Physical layout (2 rows × 8; power/SWD cluster at the pin-1 end):

```
row A (odd):   1     3     5     7     9    11    13    15
               SWDCLK SWDIO IN7   IN6   IN5   NC    IN4   IN3
row B (even):  2     4     6     8    10    12    14    16
               3V3   CHG   GND   NC    REF   IN0   IN1   IN2
              └ pin-1 end ┘                        └ far end ┘
```

- **3V3 = pin 2, GND = pin 6** → measure the rail across these.
- **SWD = pins 1 (CLK) & 3 (DIO)**; **BAT/charge = pin 4**.
- Pins 5,7,9,12,13,14,15,16 = electrode inputs **IN0–IN7**; pin 10 = **REF**; pins 8,11 = NC.

---

## 2. U2 — nRF52832 BLE MCU (QFN-48)

| Pin | Name | Net | | Pin | Name | Net |
|---:|---|---|---|---:|---|---|
| 1 | DEC1 | — | | 25 | **SWDCLK** | SWDCLK |
| 2 | P0.00/XL1 | — | | 26 | **SWDIO** | SWDIO |
| 3 | P0.01/XL2 | — | | 27 | P0.22 | — |
| 4 | P0.02/AIN0 | — | | 28 | P0.23 | — |
| 5 | P0.03/AIN1 | — | | 29 | P0.24 | — |
| 6 | P0.04/AIN2 | — | | 30 | ANT | (antenna) |
| 7 | P0.05/AIN3 | — | | 31 | VSS | GND |
| 8 | P0.06 | — | | 32 | DEC2 | — |
| 9 | P0.07 | — | | 33 | DEC3 | — |
| 10 | P0.08 | — | | 34 | XC1 | (32 MHz X1) |
| 11 | P0.09/NFC1 | — | | 35 | XC2 | (32 MHz X1) |
| 12 | P0.10/NFC2 | — | | 36 | VDD | 3V3* |
| 13 | VDD | 3V3* | | 37 | P0.25 | LED-R |
| 14 | P0.11 | SPI_MOSI | | 38 | P0.26 | LED-G |
| 15 | P0.12 | SPI_MISO | | 39 | P0.27 | LED-B |
| 16 | P0.13 | SPI_SCLK | | 40 | P0.28/AN4 | LED2 |
| 17 | P0.14/TRACEDATA3 | SPI_CS | | 41 | P0.29/AN5 | LED3 |
| 18 | P0.15/TRACEDATA2 | SD_SPI_MOSI | | 42 | P0.30/AN6 | LED4 |
| 19 | P0.16/TRACEDATA1 | SD_SPI_MISO | | 43 | P0.31/AN7 | — |
| 20 | P0.17 | SD_SPI_SCLK | | 44 | NC | — |
| 21 | P0.18/SWO | SD_SPI_CS | | 45 | VSS | GND |
| 22 | P0.19 | — | | 46 | DEC4 | DEC4 |
| 23 | P0.20/TRACECLK | — | | 47 | DCC | (DC/DC) |
| 24 | P0.21/nRESET | — | | 48 | VDD | 3V3* |
| | | | | 49 | EP | GND |

- **SPI (to RHD amp):** MOSI=P0.11(14), MISO=P0.12(15), SCLK=P0.13(16), CS=P0.14(17).
- **SD_SPI (to flash):** MOSI=P0.15(18), MISO=P0.16(19), SCLK=P0.17(20), CS=P0.18(21).
- **LEDs:** P0.25–P0.30 (pins 37–42) drive the two RGB LEDs via R1–R7.

---

## 3. U1 — RHD2216 Intan 16-ch bio-amplifier (QFN-56)

| Pin | Name | Net | | Pin | Name | Net |
|---:|---|---|---|---:|---|---|
| 1 | IN4+ | IN4 | | 19 | !CS!+ | SPI_CS |
| 3 | IN3+ | IN3 | | 21 | SCLK+ | SPI_SCLK |
| 5 | IN2+ | IN2 | | 23 | MOSI+ | SPI_MOSI |
| 7 | IN1+ | IN1 | | 25 | MISO+ | SPI_MISO |
| 9 | IN0+ | IN0 | | 13/26/31 | VDD1/2/3 | 3V3 |
| 2/8/56 | IN3-/IN0-/IN4- | REF | | 10/11/17/32 | GND0/1/3/VESD | GND |
| 51 | IN7+ | IN7 | | 53 | IN6+ | IN6 |
| 55 | IN5+ | IN5 | | CENTER | CTR (EP) | GND |

- Analog electrode channels IN0–IN7 arrive from **P1**; negative inputs tie to **REF**.
- SPI bus shared with the nRF (SPI_MOSI/MISO/SCLK/CS). IN8–IN15 present but unused on this build.

---

## 4. U3 — W25Q128 SPI flash (WSON-8)

| Pin | Name | Net | | Pin | Name | Net |
|---:|---|---|---|---:|---|---|
| 1 | #CS | SD_SPI_CS | | 5 | DI/IO0 | SD_SPI_MOSI |
| 2 | DO/IO1 | SD_SPI_MISO | | 6 | CLK | SD_SPI_SCLK |
| 3 | #WP/IO2 | — | | 7 | #HOLD/IO3 | — |
| **4** | GND | **GND** | | **8** | VCC | **3V3** |

> Easy 3V3 probe point: flash **pin 8 (VCC) ↔ pin 4 (GND)** on the same chip.

---

## 5. Power section (sheet POWER)

**U4 — MIC23050 buck (SOT-23-5)**

| Pin | Name | Net |
|---:|---|---|
| 1 | VIN | BAT |
| 2 | GND | GND |
| 3 | EN | (from SW1) |
| 4 | FB | (feedback) |
| 5 | SW | → L2 → 3V3 |

**SW1 — power switch:** pin 2 = BAT, pin 1 = → U4 EN (gates the regulator).
**P2 — battery connector:** pin 1 = BAT (+), pin 2 = GND (−).
**L2 (4.7 µH)** = buck inductor (U4 SW → 3V3); **C14 (10 µF)** = 3V3 output cap.

---

## 6. Charger / dock board (`PCB_INTAN_Charger`) — 5-slot dock

Not the headstage; the board the headstage plugs into (has the J-Link path). Useful probe points:

**J-Link 20-pin IDC (ARM SWD):** pin 1/2 = **VDD (=3V3, this is VTref)**, pin 7 = SWDIO, pin 9 = SWCLK, pins 3/4/5/6/8… = **GND**.

**`VOLT_TP` voltage test points (×5, one per slot P1–P5):** **pin 1 = that slot's battery (Px+)**, **pin 2 = GND** — largest exposed pads on the board (best ground reference; and read ~4.19 V on pin 1 for a charged headstage).

**Slot connectors (2×8, mate with headstage P1):** same pinout as [§1](#1-p1--2x8-edge-connector-external-interface--dock).

---

*Generated from the EasyEDA JSON via netlist tracing (union-find over wires, named by net-labels + power flags). Nets shown `—` are local/unlabeled; `*` inferred from a VDD/VSS pin name on this single-rail board.*
