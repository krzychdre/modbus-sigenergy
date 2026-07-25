# Sigenergy Modbus Protocol V2.5 - Markdown conversion

**Source release:** 2025-02-19 (Version V2.5)
**Source document:** `Sigenergy_Modbus_Protocol_V2.5_EN.pdf`
**Supersedes:** `Sigenergy_Modbus_Protocol_V1.7.md` (2024-04-09) — kept in the repo for diff/history.
**Purpose:** searchable Markdown conversion for implementation work. This is not an official Sigenergy publication.

> Verify register availability and semantics against the live firmware before enabling writes. The applicable-model columns matter: some registers exist only on `Hybrid Inv.` and others only on `PV Inv.`.

## What changed since V1.7 (relevant to this project)

- **Plant ESS detail added** (unit 247): `ESS rated energy capacity` 30083, `ESS charge Cut-Off SOC` 30085, `ESS discharge Cut-Off SOC` 30086, `Plant ESS SOH` 30087, `General Alarm5` 30072.
- **Battery cluster min/max added** (inverter 1-246): `Max/Min battery cluster temperature` 30620/30621, `Max/Min battery cell voltage` 30622/30623, `Alarm5` 30609. Good for cell-imbalance and thermal monitoring.
- **Grid-point and PCS power limits added** (holding, unit 247): `[Grid Point] Max export/import limitation` 40038/40040, `PCS max export/import limitation` 40042/40044.
- **Per-inverter remote EMS dispatch added** (holding, unit 1-246): 41500-41507.
- **Inverter energy banks REMOVED**: V1.7 registers `Daily/Accumulated export energy` (30554/30556) and `Daily/Accumulated import energy` (30560/30562) are now **Reserved** in V2.5 (30554, qty 12). This corroborates the empirical finding that those banks measured the inverter AC port, not the grid CT meter — grid import/export are integrated from instantaneous power instead. See project notes.
- **New device classes**: AC-Charger (EVAC) sections 5.5/5.6 and Appendices 7-10; DC-Charger alarm Appendix 11. Not present on a plain hybrid + battery install.
- **High PV-count support**: PV5-PV16 voltage/current 31042-31065 (only `Sigen PV` M1 / large models; `MPPT count 4-8, PV count 8-16`).

## Revision history

| Version | Date | Change |
|---|---|---|
| V1.0-V1.8 | 2023-08-15 → 2024-08-05 | Interaction timeout; plant-wide power control; alarm severity; phase-power & mode registers; DC-Charger registers; remote EMS/ESS control; multi-device addressing |
| V2.0 | 2024-10-14 | AC-Charger model + registers; AC-Charger system state & alarm appendices; DC-Charger alarm appendix; RTU frame / PDU examples reworked |
| V2.1 | 2024-10-30 | "Applicable model abbreviation"; applicable-model columns in ch.5; communication-interface descriptions; PV registers; alarm-code name changes |
| V2.2 | 2024-11-28 | Plant broadcast address; inverter-level power-control registers; plant parameter register changes |
| V2.3 | 2024-12-09 | New applicable models |
| V2.4 | 2025-02-05 | Inverter register tweaks |
| V2.5 | 2025-02-19 | Plant ESS registers, two grid-point + two PCS power-control registers; holding-register comment fixes; hybrid inverter battery temperature/voltage registers |

## 1. Introduction

Standard Modbus application protocol over RS485, Fast Ethernet, WLAN, optical fibre and 4G.

- To address an **individual device**, send frames to that device's Modbus slave address (unique per plant, set in the app).
- To address **plant information / control plant behaviour**, use slave address **247** ("plant address").
- To control plant behaviour **without a reply**, use slave address **0** ("plant broadcast address"). The device executes but does not reply.

## 2. Applicable models

| Abbreviation | Models | Notes |
|---|---|---|
| Hybrid Inv. | SigenStor EC (3.0-12.0) SP; Sigen Hybrid (3.0-6.0) SP; Sigen Hybrid (5.0-30.0) TP; SigenStor EC (5.0-30.0) TP/TPLV; Sigen PV (50-125) M1-HYA; PG Controller (3.8-11.4) | MPPT 2-4 (M1-HYA: 4-8), PV 2-4 (M1-HYA: 8-16) |
| EVAC | Sigen EVAC (7/11/22) 4G T2 WH; Sigen EVAC (7/11/22) 4G T2SH WH; PG EVAC (9.6/11.5) | AC charger |
| PV Inv. | Sigen PV Max (3.0-6.0) SP; Sigen PV Max (5.0-25.0) TP; Sigen PV (50-125) M1 | MPPT 2-4 (M1: 4-8), PV 2-4 (M1: 8-16) |

In the register tables the **Hybrid** and **PV** columns mark which abbreviation may access each register (√).

## 3. Communication interfaces

### 3.1 RS485 (not supported on EVAC)

| Parameter | Value |
|---|---|
| Transfer mode | Modbus RTU |
| Communication | Half duplex |
| Baud rate | 9600 bit/s default |
| Framing | 1 start, 8 data, no parity, 1 stop (8N1) |

One RS485 connection to any `Hybrid Inv.`/`PV Inv.` in the plant can reach every device (by slave address).

### 3.2 Fast Ethernet/WLAN/optical fibre/4G — TCP server

| Parameter | Value |
|---|---|
| Transfer mode | Modbus TCP |
| Communication | Full duplex |
| Link-layer role | TCP Server |
| Application role | Slave |
| Port | 502 |

### 3.3 Fast Ethernet/WLAN/optical fibre/4G* — TCP client

Same as 3.2 but Link-layer role **TCP Client**, port **custom**. *If 4G is the only media, only one inverter can connect to a third-party cloud as a client.

## 4. Technical terms

- Access plant address: **247**. Plant broadcast address: **0**. Slave address range: **1-246**.
- Types: U16/U32/U64 unsigned; S16/S32 signed; STRING is ASCII.
- **RO**: read only, "only support 0x04 command". **WO**: write only, "only support 0x06 command". **RW**: "support 0x04, 0x06, 0x10 command".
- Minimum request period: **1000 ms**; unicast response timeout: **1000 ms**.
- Critical alarm: device enters fault mode and stops (auto-clears when the condition clears). General alarm: device keeps running, possibly at reduced capacity.

### Scaling

Engineering value = `raw / gain`. Example: raw `12345`, gain `1000`, unit kW = `12.345 kW`.

### Function-code note (persists in V2.5)

Section 4.1 says RO registers "only support 0x04", while section 6.1's table labels `0x03 = Read Read-only Register (RO)` and `0x04 = Read Holding Register (RW/WO)`. In practice the 30xxx read-only (input) registers are read with **0x04** — matching section 4.1 and standard Modbus input-register convention — which is what this project uses (`read_function: input`). The 6.1 table appears to be an internal doc inconsistency.

## 5. Register address definition

### 5.1 Plant running information — unit ID 247 (read-only)

Accessed only via slave address 247.

| No. | Name | Address | Qty | Access | Type | Gain | Unit | Hybrid | PV | Comment |
|---:|---|---:|---:|---|---|---:|---|:-:|:-:|---|
| 1 | System time | 30000 | 2 | RO | U32 | 1 | s | √ | √ | Epoch seconds |
| 2 | System time zone | 30002 | 1 | RO | S16 | 1 | min | √ | √ | Signed offset (was U16 in V1.7) |
| 3 | EMS work mode | 30003 | 1 | RO | U16 | N/A | - | √ | √ | 0 max self-consumption; 1 AI mode; 2 TOU; 7 remote EMS |
| 4 | [Grid sensor] status | 30004 | 1 | RO | U16 | N/A | - | √ | √ | Gateway/meter connection. 0 not connected; 1 connected |
| 5 | [Grid sensor] active power | 30005 | 2 | RO | S32 | 1000 | kW | √ | √ | At grid↔system checkpoint. >0 buy from grid; <0 sell |
| 6 | [Grid sensor] reactive power | 30007 | 2 | RO | S32 | 1000 | kVar | √ | √ | At grid↔system checkpoint |
| 7 | On/off-grid status | 30009 | 1 | RO | U16 | N/A | - | √ | | 0 on-grid; 1 off-grid auto; 2 off-grid manual |
| 8 | Max active power | 30010 | 2 | RO | U32 | 1000 | kW | √ | √ | Base value for active-power adjustments |
| 9 | Max apparent power | 30012 | 2 | RO | U32 | 1000 | kVar | √ | √ | Base value for reactive-power adjustments |
| 10 | [ESS] SOC | 30014 | 1 | RO | U16 | 10 | % | √ | | |
| 11 | Plant phase A active power | 30015 | 2 | RO | S32 | 1000 | kW | √ | √ | |
| 12 | Plant phase B active power | 30017 | 2 | RO | S32 | 1000 | kW | √ | √ | |
| 13 | Plant phase C active power | 30019 | 2 | RO | S32 | 1000 | kW | √ | √ | |
| 14 | Plant phase A reactive power | 30021 | 2 | RO | S32 | 1000 | kVar | √ | √ | |
| 15 | Plant phase B reactive power | 30023 | 2 | RO | S32 | 1000 | kVar | √ | √ | |
| 16 | Plant phase C reactive power | 30025 | 2 | RO | S32 | 1000 | kVar | √ | √ | |
| 17 | General Alarm1 | 30027 | 1 | RO | U16 | N/A | - | √ | √ | Appendix 2 |
| 18 | General Alarm2 | 30028 | 1 | RO | U16 | N/A | - | √ | √ | Appendix 3 |
| 19 | General Alarm3 | 30029 | 1 | RO | U16 | N/A | - | √ | | Appendix 4 |
| 20 | General Alarm4 | 30030 | 1 | RO | U16 | N/A | - | √ | √ | Appendix 5 |
| 21 | Plant active power | 30031 | 2 | RO | S32 | 1000 | kW | √ | √ | |
| 22 | Plant reactive power | 30033 | 2 | RO | S32 | 1000 | kVar | √ | √ | |
| 23 | Photovoltaic power | 30035 | 2 | RO | S32 | 1000 | kW | √ | √ | |
| 24 | [ESS] power | 30037 | 2 | RO | S32 | 1000 | kW | √ | | <0 discharging; >0 charging |
| 25 | Available max active power | 30039 | 2 | RO | U32 | 1000 | kW | √ | √ | Feed to AC terminal; running inverters only |
| 26 | Available min active power | 30041 | 2 | RO | U32 | 1000 | kW | √ | | Absorb from AC terminal; running inverters only |
| 27 | Available max reactive power | 30043 | 2 | RO | U32 | 1000 | kVar | √ | √ | Feed to AC terminal; running inverters only |
| 28 | Available min reactive power | 30045 | 2 | RO | U32 | 1000 | kVar | √ | √ | Absorb from AC terminal; running inverters only |
| 29 | [ESS] available max charging power | 30047 | 2 | RO | U32 | 1000 | kW | √ | | Running inverters only |
| 30 | [ESS] available max discharging power | 30049 | 2 | RO | U32 | 1000 | kW | √ | | Running inverters only |
| 31 | Plant running state | 30051 | 1 | RO | U16 | N/A | - | √ | √ | Appendix 1 |
| 32 | [Grid sensor] phase A active power | 30052 | 2 | RO | S32 | 1000 | kW | √ | √ | >0 buy; <0 sell |
| 33 | [Grid sensor] phase B active power | 30054 | 2 | RO | S32 | 1000 | kW | √ | √ | >0 buy; <0 sell |
| 34 | [Grid sensor] phase C active power | 30056 | 2 | RO | S32 | 1000 | kW | √ | √ | >0 buy; <0 sell |
| 35 | [Grid sensor] phase A reactive power | 30058 | 2 | RO | S32 | 1000 | kVar | √ | √ | |
| 36 | [Grid sensor] phase B reactive power | 30060 | 2 | RO | S32 | 1000 | kVar | √ | √ | |
| 37 | [Grid sensor] phase C reactive power | 30062 | 2 | RO | S32 | 1000 | kVar | √ | √ | |
| 38 | [ESS] available max charging capacity | 30064 | 2 | RO | U32 | 100 | kWh | √ | | Running inverters only |
| 39 | [ESS] available max discharging capacity | 30066 | 2 | RO | U32 | 100 | kWh | √ | | Running inverters only |
| 40 | [ESS] rated charging power | 30068 | 2 | RO | U32 | 1000 | kW | √ | | |
| 41 | [ESS] rated discharging power | 30070 | 2 | RO | U32 | 1000 | kW | √ | | |
| 42 | General Alarm5 | 30072 | 1 | RO | U16 | N/A | - | √ | | **New in V2.5** — Appendix 11 |
| 43 | Reserved | 30073 | 10 | RO | N/A | N/A | - | | | |
| 44 | [ESS] rated energy capacity | 30083 | 2 | RO | U32 | 100 | kWh | √ | | **New in V2.5** — total plant battery capacity |
| 45 | [ESS] charge Cut-Off SOC | 30085 | 1 | RO | U16 | 10 | % | √ | | **New in V2.5** — upper SOC limit |
| 46 | [ESS] discharge Cut-Off SOC | 30086 | 1 | RO | U16 | 10 | % | √ | | **New in V2.5** — lower SOC limit / reserve |
| 47 | [ESS] SOH | 30087 | 1 | RO | U16 | 10 | % | √ | | **New in V2.5** — capacity-weighted plant SOH |

#### 5.1a Undocumented plant register (empirically discovered)

> Not in the V2.5 source document. Found by probing live firmware (V100R001C10SPC116) against the MySigen app. Treat semantics as best-effort.

| Name | Address | Qty | Access | Type | Gain | Unit | Comment |
|---|---:|---:|---|---|---:|---|---|
| Plant daily PV generation | 30272 | 2 | RO | U32 | 100 | kWh | Matches the app's daily PV production. Grid import/export and home load have no reliable native register, so they are power-integrated. |

### 5.2 Plant parameter setting — unit ID 0/247 (holding register)

Send to address 0 (execute, no reply) or 247 (execute + reply). Power-control registers not otherwise noted take effect only when remote EMS control mode (40031) = 0.

| No. | Name | Address | Qty | Access | Type | Gain | Unit | Hybrid | PV | Comment |
|---:|---|---:|---:|---|---|---:|---|:-:|:-:|---|
| 1 | Start/Stop | 40000 | 1 | WO | U16 | N/A | - | √ | √ | 0 stop; 1 start |
| 2 | Active power fixed adjustment target | 40001 | 2 | RW | S32 | 1000 | kW | √ | √ | |
| 3 | Reactive power fixed adjustment target | 40003 | 2 | RW | S32 | 1000 | kVar | √ | √ | Range ±60×base value; global regardless of EMS mode |
| 4 | Active power percentage adjustment target | 40005 | 1 | RW | S16 | 100 | % | √ | √ | Range [-100.00, 100.00] |
| 5 | Q/S adjustment target | 40006 | 1 | RW | S16 | 100 | % | √ | √ | Range [-60.00, 60.00]; global |
| 6 | Power factor adjustment target | 40007 | 1 | RW | S16 | 1000 | N/A | √ | √ | (-1,-0.8] U [0.8,1]; grid sensor needed; global |
| 7 | Phase A active power fixed target | 40008 | 2 | RW | S32 | 1000 | kW | √ | | Output type L1/L2/L3/N only |
| 8 | Phase B active power fixed target | 40010 | 2 | RW | S32 | 1000 | kW | √ | | Output type L1/L2/L3/N only |
| 9 | Phase C active power fixed target | 40012 | 2 | RW | S32 | 1000 | kW | √ | | Output type L1/L2/L3/N only |
| 10 | Phase A reactive power fixed target | 40014 | 2 | RW | S32 | 1000 | kVar | √ | | Output type L1/L2/L3/N only |
| 11 | Phase B reactive power fixed target | 40016 | 2 | RW | S32 | 1000 | kVar | √ | | Output type L1/L2/L3/N only |
| 12 | Phase C reactive power fixed target | 40018 | 2 | RW | S32 | 1000 | kVar | √ | | Output type L1/L2/L3/N only |
| 13 | Phase A active power percentage target | 40020 | 1 | RW | S16 | 100 | % | √ | | L1/L2/L3/N only; [-100,100] |
| 14 | Phase B active power percentage target | 40021 | 1 | RW | S16 | 100 | % | √ | | L1/L2/L3/N only; [-100,100] |
| 15 | Phase C active power percentage target | 40022 | 1 | RW | S16 | 100 | % | √ | | L1/L2/L3/N only; [-100,100] |
| 16 | Phase A Q/S fixed adjustment target | 40023 | 1 | RW | S16 | 100 | % | √ | | L1/L2/L3/N only; [-60,60] |
| 17 | Phase B Q/S fixed adjustment target | 40024 | 1 | RW | S16 | 100 | % | √ | | L1/L2/L3/N only; [-60,60] |
| 18 | Phase C Q/S fixed adjustment target | 40025 | 1 | RW | S16 | 100 | % | √ | | L1/L2/L3/N only; [-60,60] |
| 19 | Reserved | 40026 | 3 | RW | N/A | N/A | - | | | Was "active power fixed/percentage upper limit" in V1.7 |
| 20 | Remote EMS enable | 40029 | 1 | RW | U16 | N/A | - | √ | √ | 0 disabled; 1 enabled. When enabled, EMS work mode (30003) switches to remote EMS |
| 21 | Independent phase power control enable | 40030 | 1 | RW | U16 | N/A | - | √ | | L1/L2/L3/N only. 0 disabled; 1 enabled |
| 22 | Remote EMS control mode | 40031 | 1 | RW | U16 | N/A | - | √ | √ | Appendix 6 |
| 23 | ESS max charging limit | 40032 | 2 | RW | U32 | 1000 | kW | √ | | [0, rated ESS charging power]; effective when mode 40031 = 3 or 4 |
| 24 | ESS max discharging limit | 40034 | 2 | RW | U32 | 1000 | kW | √ | | [0, rated ESS discharging power]; effective when mode 40031 = 5 or 6 |
| 25 | PV max power limit | 40036 | 2 | RW | U32 | 1000 | kW | √ | | Effective when mode 40031 = 3,4,5,6 |
| 26 | [Grid Point] max export limitation | 40038 | 2 | RW | U32 | 1000 | kW | √ | √ | **New in V2.5** — grid sensor needed; global |
| 27 | [Grid Point] max import limitation | 40040 | 2 | RW | U32 | 1000 | kW | √ | √ | **New in V2.5** — grid sensor needed; global |
| 28 | PCS max export limitation | 40042 | 2 | RW | U32 | 1000 | kW | √ | √ | **New in V2.5** — [0,0xFFFFFFFE]; 0xFFFFFFFF disables; else global |
| 29 | PCS max import limitation | 40044 | 2 | RW | U32 | 1000 | kW | √ | √ | **New in V2.5** — [0,0xFFFFFFFE]; 0xFFFFFFFF disables; else global |

### 5.3 Hybrid inverter running information — unit ID 1-246 (read-only)

For PV-string registers, check the model's PV count in Table 2-1.

| No. | Name | Address | Qty | Access | Type | Gain | Unit | Hybrid | PV | Comment |
|---:|---|---:|---:|---|---|---:|---|:-:|:-:|---|
| 1 | Model type | 30500 | 15 | RO | STRING | N/A | - | √ | √ | |
| 2 | Serial number | 30515 | 10 | RO | STRING | N/A | - | √ | √ | |
| 3 | Machine firmware version | 30525 | 15 | RO | STRING | N/A | - | √ | √ | |
| 4 | Rated active power | 30540 | 2 | RO | U32 | 1000 | kW | √ | √ | |
| 5 | Max apparent power | 30542 | 2 | RO | U32 | 1000 | kVA | √ | √ | |
| 6 | Max active power | 30544 | 2 | RO | U32 | 1000 | kW | √ | √ | |
| 7 | Max absorption power | 30546 | 2 | RO | U32 | 1000 | kW | √ | | |
| 8 | Rated battery capacity | 30548 | 2 | RO | U32 | 100 | kWh | √ | | |
| 9 | [ESS] rated charge power | 30550 | 2 | RO | U32 | 1000 | kW | √ | | |
| 10 | [ESS] rated discharge power | 30552 | 2 | RO | U32 | 1000 | kW | √ | | |
| 11 | Reserved | 30554 | 12 | RO | N/A | N/A | - | | | **V1.7 daily/accumulated export & import energy (30554-30565) are RESERVED in V2.5** |
| 12 | [ESS] daily charge energy | 30566 | 2 | RO | U32 | 100 | kWh | √ | | |
| 13 | [ESS] accumulated charge energy | 30568 | 4 | RO | U64 | 100 | kWh | √ | | |
| 14 | [ESS] daily discharge energy | 30572 | 2 | RO | U32 | 100 | kWh | √ | | |
| 15 | [ESS] accumulated discharge energy | 30574 | 4 | RO | U64 | 100 | kWh | √ | | |
| 16 | Running state | 30578 | 1 | RO | U16 | N/A | - | √ | √ | Appendix 1 |
| 17 | Max active power adjustment value | 30579 | 2 | RO | S32 | 1000 | kW | √ | √ | |
| 18 | Min active power adjustment value | 30581 | 2 | RO | S32 | 1000 | kW | √ | | |
| 19 | Max reactive power adj. fed to AC terminal | 30583 | 2 | RO | U32 | 1000 | kVar | √ | √ | |
| 20 | Max reactive power adj. absorbed from AC terminal | 30585 | 2 | RO | U32 | 1000 | kVar | √ | √ | |
| 21 | Active power | 30587 | 2 | RO | S32 | 1000 | kW | √ | √ | |
| 22 | Reactive power | 30589 | 2 | RO | S32 | 1000 | kVar | √ | √ | |
| 23 | [ESS] max battery charge power | 30591 | 2 | RO | U32 | 1000 | kW | √ | | |
| 24 | [ESS] max battery discharge power | 30593 | 2 | RO | U32 | 1000 | kW | √ | | |
| 25 | [ESS] available battery charge energy | 30595 | 2 | RO | U32 | 100 | kWh | √ | | |
| 26 | [ESS] available battery discharge energy | 30597 | 2 | RO | U32 | 100 | kWh | √ | | |
| 27 | [ESS] charge/discharge power | 30599 | 2 | RO | S32 | 1000 | kW | √ | | <0 discharging; >0 charging |
| 28 | [ESS] battery SOC | 30601 | 1 | RO | U16 | 10 | % | √ | | |
| 29 | [ESS] battery SOH | 30602 | 1 | RO | U16 | 10 | % | √ | | |
| 30 | [ESS] average cell temperature | 30603 | 1 | RO | S16 | 10 | ℃ | √ | | |
| 31 | [ESS] average cell voltage | 30604 | 1 | RO | U16 | 1000 | V | √ | | |
| 32 | Alarm1 | 30605 | 1 | RO | U16 | N/A | - | √ | √ | Appendix 2 |
| 33 | Alarm2 | 30606 | 1 | RO | U16 | N/A | - | √ | √ | Appendix 3 |
| 34 | Alarm3 | 30607 | 1 | RO | U16 | N/A | - | √ | | Appendix 4 |
| 35 | Alarm4 | 30608 | 1 | RO | U16 | N/A | - | √ | √ | Appendix 5 |
| 36 | Alarm5 | 30609 | 1 | RO | U16 | N/A | - | √ | | **New in V2.5** — Appendix 11 |
| 37 | Reserved | 30610 | 10 | RO | N/A | N/A | - | | | |
| 38 | [ESS] max battery (cluster) temperature | 30620 | 1 | RO | S16 | 10 | ℃ | √ | | **New in V2.5** |
| 39 | [ESS] min battery (cluster) temperature | 30621 | 1 | RO | S16 | 10 | ℃ | √ | | **New in V2.5** |
| 40 | [ESS] max battery (cluster) cell voltage | 30622 | 1 | RO | U16 | 1000 | V | √ | | **New in V2.5** |
| 41 | [ESS] min battery (cluster) cell voltage | 30623 | 1 | RO | U16 | 1000 | V | √ | | **New in V2.5** |
| 42 | Rated grid voltage | 31000 | 1 | RO | U16 | 10 | V | √ | √ | |
| 43 | Rated grid frequency | 31001 | 1 | RO | U16 | 100 | Hz | √ | √ | |
| 44 | Grid frequency | 31002 | 1 | RO | U16 | 100 | Hz | √ | √ | |
| 45 | [PCS] internal temperature | 31003 | 1 | RO | S16 | 10 | ℃ | √ | √ | |
| 46 | Output type | 31004 | 1 | RO | U16 | N/A | - | √ | √ | 0 L/N; 1 L1/L2/L3; 2 L1/L2/L3/N; 3 L1/L2/N |
| 47 | A-B line voltage | 31005 | 2 | RO | U32 | 100 | V | √ | √ | Invalid when output type L/N or L1/L2/N |
| 48 | B-C line voltage | 31007 | 2 | RO | U32 | 100 | V | √ | √ | |
| 49 | C-A line voltage | 31009 | 2 | RO | U32 | 100 | V | √ | √ | |
| 50 | Phase A voltage | 31011 | 2 | RO | U32 | 100 | V | √ | √ | L/N output: "phase voltage" |
| 51 | Phase B voltage | 31013 | 2 | RO | U32 | 100 | V | √ | √ | |
| 52 | Phase C voltage | 31015 | 2 | RO | U32 | 100 | V | √ | √ | |
| 53 | Phase A current | 31017 | 2 | RO | S32 | 100 | A | √ | √ | L/N output: "phase current" |
| 54 | Phase B current | 31019 | 2 | RO | S32 | 100 | A | √ | √ | |
| 55 | Phase C current | 31021 | 2 | RO | S32 | 100 | A | √ | √ | |
| 56 | Power factor | 31023 | 1 | RO | U16 | 1000 | N/A | √ | √ | |
| 57 | PACK count | 31024 | 1 | RO | U16 | 1 | N/A | √ | | |
| 58 | PV string count | 31025 | 1 | RO | U16 | 1 | N/A | √ | √ | |
| 59 | MPPT count | 31026 | 1 | RO | U16 | 1 | N/A | √ | √ | |
| 60 | PV1 voltage | 31027 | 1 | RO | S16 | 10 | V | √ | √ | |
| 61 | PV1 current | 31028 | 1 | RO | S16 | 100 | A | √ | √ | |
| 62 | PV2 voltage | 31029 | 1 | RO | S16 | 10 | V | √ | √ | |
| 63 | PV2 current | 31030 | 1 | RO | S16 | 100 | A | √ | √ | |
| 64 | PV3 voltage | 31031 | 1 | RO | S16 | 10 | V | √ | √ | |
| 65 | PV3 current | 31032 | 1 | RO | S16 | 100 | A | √ | √ | |
| 66 | PV4 voltage | 31033 | 1 | RO | S16 | 10 | V | √ | √ | |
| 67 | PV4 current | 31034 | 1 | RO | S16 | 100 | A | √ | √ | |
| 68 | PV power | 31035 | 2 | RO | S32 | 1000 | kW | √ | √ | |
| 69 | Insulation resistance | 31037 | 1 | RO | U16 | 1000 | MΩ | √ | √ | |
| 70 | Startup time | 31038 | 2 | RO | U32 | 1 | s | √ | √ | Epoch seconds |
| 71 | Shutdown time | 31040 | 2 | RO | U32 | 1 | s | √ | √ | Epoch seconds |
| 72-95 | PV5-PV16 voltage/current | 31042-31065 | 1 each | RO | S16 | 10 (V) / 100 (A) | V/A | √ | √ | **New in V2.5** — even address = voltage (gain 10), odd = current (gain 100). Only on models with PV count > 4 |

DC-Charger running info (unit 1-246, on capable inverters):

| No. | Name | Address | Qty | Access | Type | Gain | Unit | Comment |
|---:|---|---:|---:|---|---|---:|---|---|
| 96 | [DC Charger] vehicle battery voltage | 31500 | 1 | RO | U16 | 10 | V | |
| 97 | [DC Charger] charging current | 31501 | 1 | RO | U16 | 10 | A | |
| 98 | [DC Charger] output power | 31502 | 2 | RO | S32 | 1000 | kW | |
| 99 | [DC Charger] vehicle SOC | 31504 | 1 | RO | U16 | 10 | % | |
| 100 | [DC Charger] current charging capacity | 31505 | 2 | RO | U32 | 100 | kWh | Single session |
| 101 | [DC Charger] current charging duration | 31507 | 2 | RO | U32 | 1 | s | Single session |

### 5.4 Hybrid inverter parameter setting — unit ID 1-246 (holding register)

| No. | Name | Address | Qty | Access | Type | Gain | Unit | Hybrid | PV | Comment |
|---:|---|---:|---:|---|---|---:|---|:-:|:-:|---|
| 1 | Start/Stop | 40500 | 1 | WO | U16 | N/A | - | √ | √ | 0 stop; 1 start |
| 2 | Grid code | 40501 | 1 | RW | U16 | N/A | - | √ | √ | |
| 3 | [DC Charger] Start/Stop | 41000 | 1 | WO | U16 | N/A | - | √ | | 0 start; 1 stop |
| 4 | Remote EMS dispatch enable | 41500 | 1 | RW | U16 | N/A | - | √ | | **New in V2.5** — 0 disabled; 1 enabled. Enabled inverter only reacts to power commands in 41501/41503/41505/41506/41507 |
| 5 | Active power fixed value adjustment | 41501 | 2 | RW | S32 | 1000 | kW | √ | | **New in V2.5** |
| 6 | Reactive power fixed value adjustment | 41503 | 2 | RW | S32 | 1000 | kVar | √ | | **New in V2.5** |
| 7 | Active power percentage adjustment | 41505 | 1 | RW | S16 | 100 | % | √ | | **New in V2.5** |
| 8 | Reactive power Q/S adjustment | 41506 | 1 | RW | S16 | 100 | % | √ | | **New in V2.5** |
| 9 | Power factor adjustment | 41507 | 1 | RW | S16 | 1000 | N/A | √ | | **New in V2.5** |

### 5.5 AC-Charger running information — unit ID 1-246, EVAC only (read-only) — new in V2.x

| No. | Name | Address | Qty | Access | Type | Gain | Unit | Comment |
|---:|---|---:|---:|---|---|---:|---|---|
| 1 | System state | 32000 | 1 | RO | U16 | N/A | - | IEC 61851-1 states; Appendix 7 |
| 2 | Total energy consumed | 32001 | 2 | RO | U32 | 100 | kWh | |
| 3 | Charging power | 32003 | 2 | RO | S32 | 1000 | kW | |
| 4 | Rated power | 32005 | 2 | RO | U32 | 1000 | kW | |
| 5 | Rated current | 32007 | 2 | RO | S32 | 100 | A | |
| 6 | Rated voltage | 32009 | 1 | RO | U16 | 10 | V | |
| 7 | AC-Charger input breaker rated current | 32010 | 2 | RO | S32 | 100 | A | |
| 8 | Alarm1 | 32012 | 1 | RO | U16 | N/A | - | Appendix 8 |
| 9 | Alarm2 | 32013 | 1 | RO | U16 | N/A | - | Appendix 9 |
| 10 | Alarm3 | 32014 | 1 | RO | U16 | N/A | - | Appendix 10 |

### 5.6 AC-Charger parameter setting — unit ID 1-246, EVAC only (holding register)

| No. | Name | Address | Qty | Access | Type | Gain | Unit | Comment |
|---:|---|---:|---:|---|---|---:|---|---|
| 1 | Start/Stop | 42000 | 1 | WO | U16 | N/A | - | 0 start; 1 stop |
| 2 | Charger output current | 42001 | 2 | RW | U32 | 100 | N/A | [6, X]; X = min(rated current, input breaker rated current) |

## 6. Modbus command overview

RTU frame: `Slave Address (1B) | PDU (X) | CRC16 (2B, little-endian byte order)`.
TCP frame: `Transaction ID (2B) | Protocol (2B, 0=Modbus) | Length (2B) | Slave Address (1B) | PDU`.

| Function | Code | Purpose |
|---|---:|---|
| Read read-only register (RO) | 0x03 | Per section 6.1 table (see function-code note above) |
| Read holding register (RW/WO) | 0x04 | |
| Write single register | 0x06 | |
| Write multiple registers | 0x10 | 1-123 registers |

Read quantity limit: 1-124 registers. TCP port 502.

### Exception codes

| Code | Name | Meaning |
|---:|---|---|
| 0x01 | Illegal function | Function code not allowable / device in wrong state |
| 0x02 | Illegal data address | Address (or address+length) not allowable |
| 0x03 | Illegal data value | Value/structure invalid (not a range check on stored values) |
| 0x04 | Slave device failure | Unrecoverable error while performing the action |

Error responses set the high bit of the function code: 0x83 (read RO), 0x84 (read holding), 0x86 (write single), 0x90 (write multiple).

## Appendix 1 — Running state

| State | Value |
|---|---:|
| Standby | 0x00 |
| Running | 0x01 |
| Fault | 0x02 |
| Shutdown | 0x03 |

## Appendix 2 — PCS alarm code 1 (register bitmask)

| Code | Description | Bit | Severity |
|---:|---|---:|---|
| 1001 | Software version mismatch | 0 | Critical |
| 1002 | Low insulation resistance | 1 | Critical |
| 1003 | Over-temperature | 2 | Critical |
| 1004 | Equipment fault | 3 | Critical |
| 1005 | System grounding fault | 4 | General |
| 1006 | PV string over-voltage | 5 | Critical |
| 1007 | PV string reversely connected | 6 | Critical |
| 1008 | PV string back-filling | 7 | Critical |
| 1009 | AFCI fault | 8 | Critical |
| 1010 | Grid power outage | 9 | Critical |
| 1011 | Grid over-voltage | 10 | Critical |
| 1012 | Grid under-voltage | 11 | Critical |
| 1013 | Grid over-frequency | 12 | Critical |
| 1014 | Grid under-frequency | 13 | Critical |
| 1015 | Grid voltage imbalance | 14 | Critical |
| 1016 | DC component of output current out of limit | 15 | Critical |

## Appendix 3 — PCS alarm code 2

| Code | Description | Bit | Severity |
|---:|---|---:|---|
| 1017 | Leak current out of limit | 0 | Critical |
| 1018 | Communication abnormal | 1 | General |
| 1019 | System internal protection | 2 | Critical |
| 1020 | AFCI self-checking circuit fault | 3 | Critical |
| 1021 | Off-grid protection | 4 | Critical |
| 1022 | Manual operation protection | 5 | Critical |
| 1024 | Abnormal phase sequence | 7 | Critical |
| 1025 | Short circuit to PE | 8 | Critical |
| 1026 | Soft start failure | 9 | Critical |

## Appendix 4 — ESS alarm code

| Code | Description | Bit | Severity |
|---:|---|---:|---|
| 2001 | Software version mismatch | 0 | Critical |
| 2002 | Energy-storage module low insulation resistance to ground | 1 | General |
| 2003 | Temperature too high | 2 | Critical |
| 2004 | Equipment fault | 3 | Critical |
| 2005 | Under-temperature | 4 | Critical |
| 2008 | Internal protection | 5 | Critical |
| 2009 | Thermal runaway | 6 | Critical |

## Appendix 5 — Gateway alarm code

| Code | Description | Bit | Severity |
|---:|---|---:|---|
| 3001 | Software version mismatch | 0 | Critical |
| 3002 | Temperature too high | 1 | Critical |
| 3003 | Equipment fault | 2 | Critical |
| 3004 | Excessive leakage current in off-grid output | 3 | Critical |
| 3005 | N-line grounding fault | 4 | Critical |
| 3006 | Abnormal phase sequence of grid wiring | 5 | Critical |
| 3007 | Abnormal phase sequence of inverter wiring | 6 | Critical |
| 3008 | Grid phase loss | 7 | Critical |

## Appendix 6 — Remote EMS control modes (register 40031)

| Value | Mode |
|---:|---|
| 0x00 | PCS remote control |
| 0x01 | Standby |
| 0x02 | Maximum self-consumption |
| 0x03 | Command charging — consume grid power first |
| 0x04 | Command charging — consume PV power first |
| 0x05 | Command discharging — output from PV first |
| 0x06 | Command discharging — output from ESS first |

## Appendix 7 — AC-Charger system state (IEC 61851-1)

| State | Value |
|---|---:|
| System init | 0x00 |
| A1/A2 | 0x01 |
| B1 | 0x02 |
| B2 | 0x03 |
| C1 | 0x04 |
| C2 | 0x05 |
| F | 0x06 |
| E | 0x07 |

## Appendix 8 — AC-Charger alarm code 1

| Code | Description | Bit | Severity |
|---:|---|---:|---|
| 5001_1 | Grid overvoltage | 0 | Critical |
| 5001_2 | Grid undervoltage | 1 | Critical |
| 5001_3 | Overload | 2 | Critical |
| 5001_4 | Short circuit | 3 | Critical |
| 5001_5 | Charging output overcurrent | 4 | Critical |
| 5001_6 | Leak current out of limit | 5 | Critical |
| 5001_7 | Grounding fault | 6 | Critical |
| 5001_8 | Abnormal phase sequence of grid wiring | 7 | Critical |
| 5001_9 | PEN fault | 8 | Critical |

## Appendix 9 — AC-Charger alarm code 2

| Code | Description | Bit | Severity |
|---:|---|---:|---|
| 5002_1 | Leak current detection circuit fault | 0 | Critical |
| 5002_2 | Relay stuck | 1 | Critical |
| 5002_3 | Pilot circuit fault | 2 | Critical |
| 5002_4 | Auxiliary power supply module fault | 3 | Critical |
| 5002_5 | Electric lock fault | 4 | Critical |
| 5002_6 | Lamp panel communication fault | 5 | General |

## Appendix 10 — AC-Charger alarm code 3

| Code | Description | Bit | Severity |
|---:|---|---:|---|
| 5003 | Too high internal temperature | 0 | Critical |
| 5004 | Charging cable fault | 1 | Critical |
| 5005 | Meter communication fault | 2 | General |

## Appendix 11 — DC-Charger alarm code

| Code | Description | Bit | Severity |
|---:|---|---:|---|
| 5101 | Software version mismatch | 0 | Critical |
| 5102 | Low insulation resistance to ground | 1 | Critical |
| 5103 | Over-temperature | 2 | Critical |
| 5104 | Equipment fault | 3 | Critical |
| 5105 | Charging fault | 4 | Critical |
| 5106 | Equipment protection | 5 | Critical |
</content>
</invoke>
