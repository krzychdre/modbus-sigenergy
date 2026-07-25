# Sigenergy Modbus → one MQTT JSON document

The service polls three tiers and publishes one retained JSON document to `sigenergy/status`:

- **fast** (default 3 s): current power, grid sensor, EMS mode, SOC, phase voltages/currents, PV strings, frequency, temperature and alarms;
- **slow** (default 60 s): daily/accumulated energies, available active/reactive/ESS capability, SOH/cell metrics, insulation resistance and timestamps;
- **static** (startup and default every 3600 s): identity, firmware, ratings, topology and nominal grid data.

Slow and static values are cached and included in every fast MQTT snapshot. The payload intentionally excludes raw register values, unit maps and register metadata. Units are part of field names.

## Daily energy (MySigen-style Sankey)

`plant.energy.daily.*` consolidates the six daily energy flows the MySigen app shows, sourced as a hybrid of native counters and power integration:

| field | source |
| --- | --- |
| `plant.energy.daily.pv_kwh` | native plant register 30272 (U32, gain 100) — undocumented in the Modbus protocol (V1.7 and V2.5), present on current firmware |
| `plant.energy.daily.battery_charge_kwh` | mirror of `inverter.energy.daily.battery_charge_kwh` (native 30566) |
| `plant.energy.daily.battery_discharge_kwh` | mirror of `inverter.energy.daily.battery_discharge_kwh` (native 30572) |
| `plant.energy.daily.grid_import_kwh` | integrated from `plant.grid_sensor.active_power_kw` (30005) |
| `plant.energy.daily.grid_export_kwh` | integrated from `plant.grid_sensor.active_power_kw` (30005) |
| `plant.energy.daily.home_load_kwh` | integrated from `home = pv + grid - ess` |

`plant.energy.daily.integrated` repeats the six flows with the integrated values for PV and battery too, so native vs integrated can be compared to validate accuracy.

Grid import/export and home load have no reliable native register that matches the app's grid-CT meter, so they are produced by integrating the instantaneous plant powers sampled on the fast tier. Sign conventions from the V1.7 protocol: 30005 `>0 import` (grid→house), `<0 export`; 30037 `>0 charging` (house→battery), `<0 discharging`. Home load is clamped at ≥ 0.

### `energy_integration` config

```yaml
energy_integration:
  enabled: true
  state_file: /var/lib/sigenergy-modbus-mqtt/energy_daily.json
  timezone: Europe/Warsaw
  max_sample_gap_seconds: 30
```

`state_file` is written atomically each fast cycle (temp file + `os.replace`) and created if missing. Accumulators reset at local midnight in `timezone`. A sample whose `dt` exceeds `max_sample_gap_seconds` (stalled loop, restart gap) is skipped — downtime is genuinely unmeasured and is **not** backfilled. Restarting the service therefore produces a small gap in integrated totals for the current day.

## Run

```bash
cp config.example.yaml config.yaml
# edit Modbus IP, MQTT password and certificate path
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python sigenergy_modbus_mqtt.py -c config.yaml -v
```

Check the retained payload:

```bash
mosquitto_sub -h HOST -p 8883 --cafile /etc/sigenergy-modbus-mqtt/mqttca.crt --insecure   -u USER -P 'PASSWORD' -t sigenergy/status -C 1 | jq
```

### Docker

```bash
cp config.example.yaml config.yaml
# edit Modbus IP, MQTT host/user/password and certificate path
docker compose up -d --build
```

`docker-compose.yml` mounts three paths:

- `./config.yaml` (read-only) — your configuration; kept out of the image and git (see `.gitignore`);
- `/etc/sigenergy-modbus-mqtt` (read-only) — the MQTT CA certificate directory referenced by `mqtt.tls.ca_cert`;
- the named volume `sigenergy-state` → `/var/lib/sigenergy-modbus-mqtt` (writable) — persists `energy_daily.json` so the daily energy accumulators survive container restarts and rebuilds. If you change `energy_integration.state_file`, keep it under this directory (or adjust the mount to match).

## Payload structure

```json
{
  "timestamp": "2026-07-24T12:45:00Z",
  "online": true,
  "updated_at": {"fast":"...","slow":"...","static":"..."},
  "age_seconds": {"fast":0.0,"slow":12.4,"static":812.4},
  "data": {
    "plant": {"ems":{},"grid_sensor":{},"ess":{},"pv":{},"power":{},"energy":{"daily":{"pv_kwh":0.0,"grid_import_kwh":0.0,"grid_export_kwh":0.0,"home_load_kwh":0.0,"battery_charge_kwh":0.0,"battery_discharge_kwh":0.0,"integrated":{}}}},
    "inverter": {"identity":{},"ratings":{},"grid":{},"ess":{},"pv":{},"energy":{}}
  },
  "errors": {}
}
```

Change `interval_seconds` values in `config.yaml` as needed. `--once` refreshes all three tiers once and publishes one complete snapshot.

## Register map & protocol reference

- `registers_full.csv` — complete register map, kept current with **Modbus protocol V2.5** (`Sigenergy_Modbus_Protocol_V2.5.md`, source PDF `Sigenergy_Modbus_Protocol_V2.5_EN.pdf`). `Sigenergy_Modbus_Protocol_V1.7.md` is retained for diff/history.
- `register_map_short.csv` — curated "interesting values" subset.
- V2.5 additions polled by this service: plant ESS capacity/SOH and charge/discharge cut-off SOC (30083–30087), and per-inverter battery cluster min/max temperature and cell voltage (30620–30623) for cell-balance and thermal monitoring.
- **Note:** the V1.7 inverter export/import energy registers 30554–30562 are **Reserved** in V2.5 (they read the inverter AC port, not the grid CT meter); grid energy is integrated instead, as described above.
