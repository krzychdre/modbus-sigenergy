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
| `plant.energy.daily.pv_kwh` | native plant register 30272 (U32, gain 100) — undocumented in the Modbus protocol (V1.7 and V2.5), present on current firmware; falls back to integrated value if missing |
| `plant.energy.daily.battery_charge_kwh` | native `inverter.energy.daily.battery_charge_kwh` (30566); falls back to integrated value if missing |
| `plant.energy.daily.battery_discharge_kwh` | native `inverter.energy.daily.battery_discharge_kwh` (30572); falls back to integrated value if missing |
| `plant.energy.daily.grid_import_kwh` | integrated from `plant.grid_sensor.active_power_kw` (30005) — no native grid-CT meter |
| `plant.energy.daily.grid_export_kwh` | integrated from `plant.grid_sensor.active_power_kw` (30005) — no native grid-CT meter |
| `plant.energy.daily.home_load_kwh` | **derived** from the daily balance, not integrated directly: `home = pv - battery_charge + battery_discharge + grid_import - grid_export`, clamped `>= 0` |

`plant.energy.daily.integrated` repeats the six flows with the integrated values for PV and battery too, so native vs integrated can be compared to validate accuracy.

`plant.energy.daily.self_sufficiency_pct` = `(home_load - grid_import) / home_load * 100`, clamped to 0..100, `0.0` when `home_load <= 0`. `plant.energy.daily.self_consumption_pct` = `(pv - grid_export) / pv * 100`, clamped to 0..100, `0.0` when `pv <= 0`.

`plant.energy.daily.date` is the local ISO date the counters belong to. `plant.energy.daily.coverage` reports how much of the current local day the accumulator actually integrated:

- `covered_hours` — hours actually integrated since local midnight (excludes samples rejected by `max_sample_gap_seconds`);
- `elapsed_hours` — hours since local midnight in the configured timezone;
- `complete` — `covered_hours >= elapsed_hours - 0.25` (15-minute tolerance).

`complete: false` means grid import/export (and therefore home load) understate the day — typically because the collector was restarted or stalled. Days where the collector ran from local midnight are complete; days with gaps are not.

### Honest caveat on grid counters

Grid import/export have no reliable native register that matches the app's grid-CT meter, so they are produced by integrating the instantaneous plant powers sampled on the fast tier. Sign conventions from the V1.7 protocol: 30005 `>0 import` (grid→house), `<0 export`; 30037 `>0 charging` (house→battery), `<0 discharging`. Native PV / battery-charge / battery-discharge counters are true-since-local-midnight; grid import/export and the derived home load are only complete for days where the collector ran from midnight (`coverage.complete`). Use `coverage.complete` to gate dashboards/alerts that depend on a full-day grid total.

### Long-period aggregates

`plant.energy.periods.<key>` aggregates the six kWh flows plus the two percentages over a rolling window ending today. The window registry is defined by `PERIODS` at the top of the module; the published keys are:

- `today` (1 day) — numerically identical to `plant.energy.daily` for the six kWh fields and the two percentages;
- `week` (7 days), `month` (30 days), `year` (365 days) — counting back from today inclusive;
- `total` — all retained history.

Each period payload has the shape:

```json
{
  "window_days": 7,
  "from": "YYYY-MM-DD",
  "to": "YYYY-MM-DD",
  "days": 7,
  "complete_days": 6,
  "pv_kwh": 0.0,
  "grid_import_kwh": 0.0,
  "grid_export_kwh": 0.0,
  "home_load_kwh": 0.0,
  "battery_charge_kwh": 0.0,
  "battery_discharge_kwh": 0.0,
  "self_sufficiency_pct": 0.0,
  "self_consumption_pct": 0.0
}
```

`window_days` is `null` for `total`. `days` is the number of daily records aggregated (history days in the window plus today's live report); `complete_days` is how many of those had `coverage.complete == true`. Percentages are recomputed from the summed totals with the same clamping rules as the daily report.

### Daily history

Finished daily reports are archived to `energy_integration.history_file` (default `<state_file dir>/energy_history.json`) as `{"days": [<daily report>, ...]}` sorted by date ascending, trimmed to `history_retention_days` (default 1100, ~3 years). Each archived day is the composed daily report (Task 1) plus its `date` and `coverage` fields. The file is written atomically with the same temp-file + `os.replace` pattern as `state_file`, and a history failure is logged and swallowed — it never kills the daemon.

Rollover without losing the last day: `state_file` also carries the last composed daily report. On load, when the stored `date` is no longer today, the stale report is handed back to the history store before the accumulators reset for the new day. The same handoff happens on the in-process midnight rollover path during a long-running collector.

### `energy_integration` config

```yaml
energy_integration:
  enabled: true
  state_file: /var/lib/sigenergy-modbus-mqtt/energy_daily.json
  history_file: /var/lib/sigenergy-modbus-mqtt/energy_history.json
  history_retention_days: 1100
  timezone: Europe/Warsaw
  max_sample_gap_seconds: 30
```

`state_file` and `history_file` are written atomically each fast cycle (temp file + `os.replace`) and created if missing. Accumulators reset at local midnight in `timezone`. A sample whose `dt` exceeds `max_sample_gap_seconds` (stalled loop, restart gap) is skipped — downtime is genuinely unmeasured and is **not** backfilled. Restarting the service therefore produces a gap in the integrated totals for the current day, reflected in `coverage.complete == false`.

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
- the named volume `sigenergy-state` → `/var/lib/sigenergy-modbus-mqtt` (writable) — persists `energy_daily.json` and `energy_history.json` so the daily energy accumulators and the daily history archive survive container restarts and rebuilds. If you change `energy_integration.state_file` or `history_file`, keep them under this directory (or adjust the mount to match).

## Payload structure

```json
{
  "timestamp": "2026-07-24T12:45:00Z",
  "online": true,
  "updated_at": {"fast":"...","slow":"...","static":"..."},
  "age_seconds": {"fast":0.0,"slow":12.4,"static":812.4},
  "data": {
    "plant": {"ems":{},"grid_sensor":{},"ess":{},"pv":{},"power":{},"energy":{"daily":{"date":"YYYY-MM-DD","pv_kwh":0.0,"grid_import_kwh":0.0,"grid_export_kwh":0.0,"home_load_kwh":0.0,"battery_charge_kwh":0.0,"battery_discharge_kwh":0.0,"self_sufficiency_pct":0.0,"self_consumption_pct":0.0,"coverage":{"covered_hours":0.0,"elapsed_hours":0.0,"complete":true},"integrated":{}},"periods":{"today":{},"week":{},"month":{},"year":{},"total":{}}}},
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
