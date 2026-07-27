#!/usr/bin/env python3
"""Sigenergy Modbus TCP -> one consolidated MQTT JSON telemetry document.

Registers are split into fast, slow and static polling tiers. Slow/static values are
cached and included in every fast snapshot. No raw register mirror is published.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import ssl
import struct
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt
import yaml
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

LOG = logging.getLogger("sigenergy-modbus-mqtt")


# --- module-level helpers -----------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def compact_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def put_path(root: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = root
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def get_path(root: dict[str, Any], path: str) -> Any:
    node = root
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


# --- type decoding (OCP registry) ---------------------------------------------

def _decode_u16(registers: list[int]) -> Any:
    return registers[0]


def _decode_s16(registers: list[int]) -> Any:
    return struct.unpack(">h", struct.pack(">H", registers[0]))[0]


def _decode_u32(registers: list[int]) -> Any:
    raw = b"".join(struct.pack(">H", v) for v in registers)
    return struct.unpack(">I", raw)[0]


def _decode_s32(registers: list[int]) -> Any:
    raw = b"".join(struct.pack(">H", v) for v in registers)
    return struct.unpack(">i", raw)[0]


def _decode_u64(registers: list[int]) -> Any:
    raw = b"".join(struct.pack(">H", v) for v in registers)
    return struct.unpack(">Q", raw)[0]


def _decode_string(registers: list[int]) -> Any:
    raw = b"".join(struct.pack(">H", v) for v in registers)
    return raw.decode("ascii", errors="replace").rstrip("\x00 ").strip()


DECODERS: dict[str, Any] = {
    "U16": _decode_u16,
    "S16": _decode_s16,
    "U32": _decode_u32,
    "S32": _decode_s32,
    "U64": _decode_u64,
    "STRING": _decode_string,
}


def decode(registers: list[int], typ: str) -> Any:
    name = typ.upper()
    decoder = DECODERS.get(name)
    if decoder is None:
        raise ValueError(f"Unsupported type: {name}")
    return decoder(registers)


# --- Modbus register reader ---------------------------------------------------

class RegisterReader:
    """Wraps a ModbusTcpClient and decodes a single register definition."""

    def __init__(self, client: ModbusTcpClient, modbus_cfg: dict[str, Any]) -> None:
        self.client = client
        self.inverter_unit_id = int(modbus_cfg.get("inverter_unit_id", 1))
        self.address_offset = int(modbus_cfg.get("address_offset", 0))
        self.read_function = str(modbus_cfg.get("read_function", "input")).lower()

    def read(self, reg: dict[str, Any]) -> Any:
        unit_id = int(reg.get("unit_id") or self.inverter_unit_id)
        address = int(reg["address"]) + self.address_offset
        count = int(reg.get("count", 1))
        kwargs = {"address": address, "count": count, "slave": unit_id}
        if self.read_function == "input":
            response = self.client.read_input_registers(**kwargs)
        else:
            response = self.client.read_holding_registers(**kwargs)
        if response.isError():
            raise ModbusException(str(response))
        value = decode(response.registers, reg["type"])
        gain = float(reg.get("gain", 1) or 1)
        if gain != 1 and not isinstance(value, str):
            value = value / gain
        enum = reg.get("enum")
        if enum:
            label = enum.get(str(value), enum.get(int(value) if isinstance(value, (int, float)) else value))
            return {"code": value, "label": label or "unknown"}
        return value

    def reset(self) -> None:
        # best-effort close so the next cycle establishes a fresh Modbus connection
        try:
            self.client.close()
        except Exception:
            pass


# --- MQTT publisher -----------------------------------------------------------

class MqttPublisher:
    """Encapsulates paho setup, connect handshake, publish, and teardown."""

    def __init__(self, mc: dict[str, Any]) -> None:
        self.mc = mc
        self.topic = mc.get("topic", "sigenergy/status")
        self.qos = int(mc.get("qos", 1))
        self.retain = bool(mc.get("retain", True))
        self.connect_timeout = float(mc.get("connect_timeout_seconds", 10))
        self.publish_timeout = float(mc.get("publish_timeout_seconds", 10))

        self._connected = threading.Event()
        self._connection_error: list[str] = []
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=mc.get("client_id", "sigenergy-modbus-mqtt"),
        )
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        # cap paho loop_start auto-reconnect backoff so a broker drop recovers quickly
        self.client.reconnect_delay_set(min_delay=1, max_delay=int(mc.get("reconnect_max_delay_seconds", 60)))

        if mc.get("username"):
            self.client.username_pw_set(mc["username"], mc.get("password", ""))

        tls = mc.get("tls", {}) or {}
        if tls.get("enabled"):
            ca = Path(tls["ca_cert"])
            if not ca.is_file():
                raise FileNotFoundError(f"MQTT CA certificate not found: {ca}")
            insecure = bool(tls.get("insecure", False))
            self.client.tls_set(
                ca_certs=str(ca),
                cert_reqs=ssl.CERT_NONE if insecure else ssl.CERT_REQUIRED,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )
            self.client.tls_insecure_set(insecure)

        self.client.will_set(
            self.topic,
            compact_json({"timestamp": utc_now(), "online": False, "reason": "connection_lost"}),
            qos=self.qos,
            retain=self.retain,
        )

    def _on_connect(self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        if reason_code == 0:
            LOG.info("Connected to MQTT broker %s:%s", self.mc["host"], self.mc.get("port", 1883))
            self._connected.set()
        else:
            self._connection_error.append(str(reason_code))
            self._connected.set()

    def _on_disconnect(self, client: Any, userdata: Any, disconnect_flags: Any, reason_code: Any, properties: Any) -> None:
        # unexpected drop only (clean disconnect has reason_code == 0); paho's loop_start auto-reconnects
        if reason_code != 0:
            self._connected.clear()
            LOG.warning("MQTT disconnected (%s); auto-reconnecting", reason_code)

    def connect(self) -> None:
        self.client.connect(self.mc["host"], int(self.mc.get("port", 1883)), 60)
        self.client.loop_start()
        if not self._connected.wait(self.connect_timeout):
            raise RuntimeError("MQTT connection timeout")
        if self._connection_error:
            raise RuntimeError(f"MQTT connection rejected: {self._connection_error[-1]}")

    def publish(self, payload: dict[str, Any]) -> None:
        info = self.client.publish(self.topic, compact_json(payload), qos=self.qos, retain=self.retain)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(mqtt.error_string(info.rc))
        info.wait_for_publish(self.publish_timeout)
        if not info.is_published():
            raise RuntimeError("MQTT publication not acknowledged")

    def close(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()


# --- energy accumulator -------------------------------------------------------

class EnergyAccumulator:
    """Persistent daily power-integration accumulator.

    Integrates instantaneous plant powers to kWh for grid import/export and home
    load, plus integrated PV / battery charge / battery discharge as cross-check
    values against the native counters. Reset at local midnight in the configured
    timezone.

    Sign conventions from V1.7 doc:
      - 30005 grid sensor active power: >0 import (grid->house), <0 export (house->grid)
      - 30035 PV power: >=0 generation
      - 30037 ESS power: >0 charging (house->battery), <0 discharging (battery->house)
    Home load balance: home = pv + grid - ess  (all in kW, import/charge positive).
    """

    FIELDS = ("grid_import_kwh", "grid_export_kwh", "home_load_kwh",
              "pv_kwh", "battery_charge_kwh", "battery_discharge_kwh")

    def __init__(self, state_file: str, tz_name: str, max_gap_s: float) -> None:
        self.state_file = Path(state_file)
        self.tz = ZoneInfo(tz_name)
        self.max_gap_s = float(max_gap_s)
        self.last_ts: float | None = None
        self.date: str | None = None
        self.totals: dict[str, float] = dict.fromkeys(self.FIELDS, 0.0)
        # seconds of the current local day actually integrated (excludes rejected samples)
        self.covered_seconds: float = 0.0
        # last composed daily report for the day we are tracking (persisted so a
        # restart across midnight can still archive the day that ended)
        self.last_daily_report: dict[str, Any] | None = None
        # set only by a midnight rollover: the finished day's report, awaiting archival
        self.stale_report: dict[str, Any] | None = None

    def _local_date(self) -> str:
        return datetime.now(self.tz).date().isoformat()

    def _local_now(self) -> datetime:
        return datetime.now(self.tz)

    def _reset_totals(self) -> None:
        self.totals = dict.fromkeys(self.FIELDS, 0.0)
        self.covered_seconds = 0.0

    def load(self) -> dict[str, Any] | None:
        """Load persisted state. Returns the stale daily report if the stored
        date is no longer today, so the caller can archive it before the
        accumulators reset for the new day. Returns None otherwise."""
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception as exc:
            LOG.warning("energy state file %s unreadable (%s); starting fresh", self.state_file, exc)
            return None
        today = self._local_date()
        stored_date = data.get("date") or today
        stale_report = data.get("last_daily_report")
        if stored_date != today:
            # stored day has passed -> start the new day at zero; hand the stale
            # composed report back so the caller can archive it into history.
            self.date = today
            self._reset_totals()
            self.last_daily_report = None
            LOG.info("energy state dated %s != today %s; reset accumulators", stored_date, today)
            return stale_report if isinstance(stale_report, dict) else None
        self.date = stored_date
        for f in self.FIELDS:
            try:
                self.totals[f] = float(data.get(f, 0.0))
            except (TypeError, ValueError):
                self.totals[f] = 0.0
        try:
            self.covered_seconds = float(data.get("covered_seconds", 0.0))
        except (TypeError, ValueError):
            self.covered_seconds = 0.0
        if isinstance(stale_report, dict):
            self.last_daily_report = stale_report
        LOG.info("energy state loaded for %s: %s", self.date, self.snapshot())
        return None

    def update(self, pv_kw: float, grid_kw: float, ess_kw: float) -> None:
        self._start_new_day_if_needed()
        hours = self._sample_hours()
        if hours is None:
            return
        # grid: >0 import, <0 export (per doc 30005)
        self._integrate_signed("grid_import_kwh", "grid_export_kwh", grid_kw, hours)
        # ess: >0 charging (house->battery), <0 discharging (battery->house) (per doc 30037)
        self._integrate_signed("battery_charge_kwh", "battery_discharge_kwh", ess_kw, hours)
        # pv: >=0 generation
        self._integrate_positive("pv_kwh", pv_kw, hours)
        # home load = pv + grid_import_signed - ess_charge_signed (clamp >= 0)
        self._integrate_positive("home_load_kwh", pv_kw + grid_kw - ess_kw, hours)
        # coverage only counts accepted samples
        self.covered_seconds += hours * 3600.0

    def take_stale_report(self) -> dict[str, Any] | None:
        """Return and clear the report of a day that has just ended. Yields a
        value only on the cycle following a midnight rollover — on every other
        cycle there is nothing to archive and this returns None."""
        rep = self.stale_report
        self.stale_report = None
        return rep

    def set_last_daily_report(self, report: dict[str, Any]) -> None:
        """Remember the most recently composed daily report so it survives a
        restart and can be archived on the next midnight rollover."""
        self.last_daily_report = report

    def _start_new_day_if_needed(self) -> bool:
        today = self._local_date()
        if self.date == today:
            return False
        if self.date is not None:
            LOG.info("local date %s -> %s; resetting daily energy accumulators", self.date, today)
        # the report we were maintaining belongs to the day that just ended
        self.stale_report, self.last_daily_report = self.last_daily_report, None
        self.date = today
        self._reset_totals()
        return True

    def _sample_hours(self) -> float | None:
        now = time.monotonic()
        if self.last_ts is None:
            self.last_ts = now
            return None
        dt = now - self.last_ts
        self.last_ts = now
        if dt <= 0:
            return None
        if dt > self.max_gap_s:
            # gap too large (stalled loop or restart) -> unmeasured; do not backfill
            LOG.debug("skipping integration sample: dt=%.1fs exceeds max_gap_s=%.0f", dt, self.max_gap_s)
            return None
        return dt / 3600.0

    def _integrate_signed(self, positive: str, negative: str, value: float, hours: float) -> None:
        if value > 0:
            self.totals[positive] += value * hours
        elif value < 0:
            self.totals[negative] += -value * hours

    def _integrate_positive(self, field: str, value: float, hours: float) -> None:
        if value > 0:
            self.totals[field] += value * hours

    def snapshot(self) -> dict[str, float]:
        return {f: round(self.totals[f], 3) for f in self.FIELDS}

    def coverage(self) -> dict[str, Any]:
        """How much of the current local day the accumulator actually integrated."""
        now = self._local_now()
        midnight = datetime.combine(now.date(), datetime.min.time(), tzinfo=self.tz)
        elapsed_hours = max(0.0, (now - midnight).total_seconds() / 3600.0)
        covered_hours = self.covered_seconds / 3600.0
        return {
            "covered_hours": round(covered_hours, 2),
            "elapsed_hours": round(elapsed_hours, 2),
            "complete": bool(covered_hours >= max(0.0, elapsed_hours - 0.25)),
        }

    def persist(self) -> None:
        snap = self.snapshot()
        snap["date"] = self.date
        snap["covered_seconds"] = self.covered_seconds
        if self.last_daily_report is not None:
            snap["last_daily_report"] = self.last_daily_report
        parent = self.state_file.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            LOG.warning("cannot create energy state dir %s (%s); state stays in-memory", parent, exc)
            return
        try:
            fd, tmp = tempfile.mkstemp(dir=str(parent), prefix=".energy_daily.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(snap, fh, ensure_ascii=False, separators=(",", ":"))
                    fh.write("\n")
                os.replace(tmp, self.state_file)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as exc:
            LOG.warning("cannot persist energy state to %s (%s); state stays in-memory", self.state_file, exc)


# --- daily energy report composer ---------------------------------------------

# Six kWh fields every daily report carries, in a stable order.
DAILY_KWH_FIELDS = (
    "pv_kwh",
    "grid_import_kwh",
    "grid_export_kwh",
    "home_load_kwh",
    "battery_charge_kwh",
    "battery_discharge_kwh",
)


def _num(value: Any) -> float | None:
    """Coerce a cache value to float, or None if missing/non-numeric."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _clamp_pct(value: float) -> float:
    """Clamp a percentage to the 0..100 range."""
    if value < 0.0:
        return 0.0
    if value > 100.0:
        return 100.0
    return value


def compose_daily_report(cache: dict[str, Any], accumulator: EnergyAccumulator) -> dict[str, Any]:
    """Compose the published daily energy report from the best source per field.

    PV, battery charge and battery discharge use the native cache counters when
    present and fall back to the accumulator's integrated values. Grid import
    and grid export are always power-integrated (no native grid-CT meter).
    Home load is derived from the daily balance so it stays consistent with the
    native counters rather than being the integrated-since-process-start value:

        home = pv - battery_charge + battery_discharge + import - export  (>= 0)

    Percentages are derived from the final per-field values with the same
    clamping rules, so they stay numerically identical between the daily report
    and the `today` period aggregate.
    """
    snap = accumulator.snapshot()
    native_pv = _num(get_path(cache, "plant.energy.daily.pv_kwh"))
    native_bc = _num(get_path(cache, "inverter.energy.daily.battery_charge_kwh"))
    native_bd = _num(get_path(cache, "inverter.energy.daily.battery_discharge_kwh"))

    pv = native_pv if native_pv is not None else snap["pv_kwh"]
    battery_charge = native_bc if native_bc is not None else snap["battery_charge_kwh"]
    battery_discharge = native_bd if native_bd is not None else snap["battery_discharge_kwh"]
    grid_import = snap["grid_import_kwh"]
    grid_export = snap["grid_export_kwh"]

    home_load = pv - battery_charge + battery_discharge + grid_import - grid_export
    if home_load < 0.0:
        home_load = 0.0

    self_suff = 0.0 if home_load <= 0.0 else _clamp_pct((home_load - grid_import) / home_load * 100.0)
    self_cons = 0.0 if pv <= 0.0 else _clamp_pct((pv - grid_export) / pv * 100.0)

    return {
        "date": accumulator.date,
        "pv_kwh": round(pv, 3),
        "grid_import_kwh": round(grid_import, 3),
        "grid_export_kwh": round(grid_export, 3),
        "home_load_kwh": round(home_load, 3),
        "battery_charge_kwh": round(battery_charge, 3),
        "battery_discharge_kwh": round(battery_discharge, 3),
        "self_sufficiency_pct": round(self_suff, 2),
        "self_consumption_pct": round(self_cons, 2),
        "coverage": accumulator.coverage(),
    }


# Period registry: name -> window length in days counting back from today
# inclusive, or None for "all history". Add a line here to publish a new period.
PERIODS: dict[str, int | None] = {
    "today": 1,
    "week": 7,
    "month": 30,
    "year": 365,
    "total": None,
}


def aggregate_periods(
    today_report: dict[str, Any],
    history: list[dict[str, Any]],
    today_date: str,
) -> dict[str, Any]:
    """Build the `plant.energy.periods` subtree from today's live report plus
    archived history days. `PERIODS` defines each window's length in days
    counting back from today inclusive; `None` means all history.
    """
    # Today is contributed by the live report, never by the archive — a record
    # for today may still be present there after an out-of-order archive, and
    # counting it twice would inflate every window.
    past = [rec for rec in history if str(rec.get("date")) < today_date]
    periods: dict[str, Any] = {}
    for key, window_days in PERIODS.items():
        window_days_int = None if window_days is None else int(window_days)
        if window_days_int is None:
            window_records = list(past)
        else:
            cutoff = _date_minus_days(today_date, window_days_int - 1)
            window_records = [rec for rec in past if str(rec.get("date")) >= cutoff]
        # today's live report always counts for the "to" edge of every window
        records: list[dict[str, Any]] = window_records + [today_report]
        summed = {f: 0.0 for f in DAILY_KWH_FIELDS}
        complete_days = 0
        for rec in records:
            for f in DAILY_KWH_FIELDS:
                v = _num(rec.get(f))
                if v is not None:
                    summed[f] += v
            cov = rec.get("coverage") or {}
            if isinstance(cov, dict) and cov.get("complete") is True:
                complete_days += 1

        pv = summed["pv_kwh"]
        grid_import = summed["grid_import_kwh"]
        grid_export = summed["grid_export_kwh"]
        home_load = summed["home_load_kwh"]
        battery_charge = summed["battery_charge_kwh"]
        battery_discharge = summed["battery_discharge_kwh"]

        self_suff = 0.0 if home_load <= 0.0 else _clamp_pct((home_load - grid_import) / home_load * 100.0)
        self_cons = 0.0 if pv <= 0.0 else _clamp_pct((pv - grid_export) / pv * 100.0)

        from_date = today_date
        to_date = today_date
        if records:
            dates = sorted(r.get("date") for r in records if isinstance(r.get("date"), str))
            if dates:
                from_date = dates[0]
                to_date = dates[-1]

        periods[key] = {
            "window_days": window_days_int,
            "from": from_date,
            "to": to_date,
            "days": len(records),
            "complete_days": complete_days,
            "pv_kwh": round(pv, 3),
            "grid_import_kwh": round(grid_import, 3),
            "grid_export_kwh": round(grid_export, 3),
            "home_load_kwh": round(home_load, 3),
            "battery_charge_kwh": round(battery_charge, 3),
            "battery_discharge_kwh": round(battery_discharge, 3),
            "self_sufficiency_pct": round(self_suff, 2),
            "self_consumption_pct": round(self_cons, 2),
        }
    return periods


def _date_minus_days(today_iso: str, days: int) -> str:
    """Return the ISO date `days` days before today_iso (inclusive window start)."""
    try:
        d = date.fromisoformat(today_iso)
    except ValueError:
        return today_iso
    return (d - timedelta(days=days)).isoformat()


# --- daily history store ------------------------------------------------------

class DailyHistoryStore:
    """Persists finished daily reports to a JSON file, trimmed to a retention
    limit. Failures are logged and swallowed so a history problem can never
    kill the polling cycle."""

    def __init__(self, history_file: str, retention_days: int) -> None:
        self.history_file = Path(history_file)
        self.retention_days = max(1, int(retention_days))
        self._cache: list[dict[str, Any]] | None = None

    def load(self) -> list[dict[str, Any]]:
        """Load history days sorted by date ascending. Cached so repeated reads
        during a single cycle don't re-read the file."""
        if self._cache is not None:
            return self._cache
        try:
            data = json.loads(self.history_file.read_text(encoding="utf-8"))
            days = data.get("days") if isinstance(data, dict) else None
            if not isinstance(days, list):
                self._cache = []
                return self._cache
            cleaned: list[dict[str, Any]] = []
            for entry in days:
                if isinstance(entry, dict) and isinstance(entry.get("date"), str):
                    cleaned.append(entry)
            cleaned.sort(key=lambda r: str(r.get("date")))
            self._cache = cleaned
            return self._cache
        except FileNotFoundError:
            self._cache = []
            return self._cache
        except Exception as exc:
            LOG.warning("history file %s unreadable (%s); starting with empty history", self.history_file, exc)
            self._cache = []
            return self._cache

    def archive(self, daily_report: dict[str, Any]) -> None:
        """Append a finished daily report, deduping by date (newest wins), then
        trim to retention and persist atomically."""
        if not isinstance(daily_report, dict) or not isinstance(daily_report.get("date"), str):
            return
        days = list(self.load())
        new_date = daily_report["date"]
        days = [d for d in days if d.get("date") != new_date]
        days.append(daily_report)
        days.sort(key=lambda r: str(r.get("date")))
        # trim to the newest retention_days entries
        if len(days) > self.retention_days:
            days = days[-self.retention_days:]
        self._cache = days
        self._persist(days)

    def _persist(self, days: list[dict[str, Any]]) -> None:
        parent = self.history_file.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            LOG.warning("cannot create history dir %s (%s); history stays in-memory", parent, exc)
            return
        try:
            fd, tmp = tempfile.mkstemp(dir=str(parent), prefix=".energy_history.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"days": days}, fh, ensure_ascii=False, separators=(",", ":"))
                    fh.write("\n")
                os.replace(tmp, self.history_file)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as exc:
            LOG.warning("cannot persist history to %s (%s); history stays in-memory", self.history_file, exc)


# --- polling tiers ------------------------------------------------------------

class TierPoller:
    """Holds per-tier cache/error/last-run state and refreshes due tiers."""

    def __init__(self, tiers: dict[str, Any], reader: RegisterReader) -> None:
        self.tiers = tiers
        self.reader = reader
        self.cache: dict[str, Any] = {}
        self.tier_last: dict[str, float] = {name: 0.0 for name in tiers}
        self.tier_updated: dict[str, Any] = {name: None for name in tiers}
        self.errors: dict[str, str] = {}

    def refresh_due(self, now: float) -> None:
        for tier_name, tier in self.tiers.items():
            if self._is_due(tier_name, tier, now):
                self._refresh_tier(tier_name, tier, now)

    def _is_due(self, tier_name: str, tier: dict[str, Any], now: float) -> bool:
        last = self.tier_last[tier_name]
        if not last:
            return True
        return now - last >= float(tier["interval_seconds"])

    def _refresh_tier(self, tier_name: str, tier: dict[str, Any], now: float) -> None:
        success = sum(self._read_register(reg) for reg in self._enabled_registers(tier))
        self.tier_last[tier_name] = now
        self.tier_updated[tier_name] = utc_now()
        LOG.info("Refreshed %s tier: %d values", tier_name, success)

    @staticmethod
    def _enabled_registers(tier: dict[str, Any]) -> list[dict[str, Any]]:
        return [reg for reg in tier.get("registers", []) if reg.get("enabled", True) is not False]

    def _read_register(self, reg: dict[str, Any]) -> bool:
        path = reg["path"]
        try:
            put_path(self.cache, path, self.reader.read(reg))
            self.errors.pop(path, None)
            return True
        except Exception as exc:
            self.errors[path] = str(exc)
            LOG.warning("%s: %s", path, exc)
            return False

    def age_seconds(self, now: float) -> dict[str, Any]:
        return {
            name: (None if self.tier_last[name] == 0 else round(now - self.tier_last[name], 1))
            for name in self.tiers
        }


# --- payload builders ---------------------------------------------------------

def _base_payload(
    mb: dict[str, Any],
    started_at: str,
    tier_updated: dict[str, Any],
    cache: dict[str, Any],
    *,
    online: bool,
) -> dict[str, Any]:
    device = {"host": mb["host"], "port": int(mb.get("port", 502))}
    if online:
        device["inverter_unit_id"] = int(mb.get("inverter_unit_id", 1))
    return {
        "timestamp": utc_now(),
        "online": online,
        "device": device,
        "started_at": started_at,
        "updated_at": tier_updated,
        "data": cache,
    }


def build_online_payload(
    mb: dict[str, Any],
    started_at: str,
    tier_updated: dict[str, Any],
    age: dict[str, Any],
    cache: dict[str, Any],
    errors: dict[str, str],
) -> dict[str, Any]:
    payload = _base_payload(mb, started_at, tier_updated, cache, online=True)
    payload["age_seconds"] = age
    payload["errors"] = errors
    return payload


def build_offline_payload(
    mb: dict[str, Any],
    started_at: str,
    tier_updated: dict[str, Any],
    cache: dict[str, Any],
) -> dict[str, Any]:
    payload = _base_payload(mb, started_at, tier_updated, cache, online=False)
    payload["errors"] = {
        "connection": f"Cannot connect to Modbus TCP {mb['host']}:{mb.get('port', 502)}"
    }
    return payload


# --- energy integration step --------------------------------------------------

def integrate_energy(
    cache: dict[str, Any],
    accumulator: EnergyAccumulator,
    history: DailyHistoryStore,
) -> None:
    """Power-integration + daily-report publishing step run each cycle when the
    accumulator is enabled. Composes the daily report from the best source per
    field, publishes it together with the coverage metadata and period
    aggregates, and persists the accumulator state."""
    pv_kw = get_path(cache, "plant.pv.power_kw")
    grid_kw = get_path(cache, "plant.grid_sensor.active_power_kw")
    ess_kw = get_path(cache, "plant.ess.power_kw")
    if not all(isinstance(v, (int, float)) for v in (pv_kw, grid_kw, ess_kw)):
        return
    accumulator.update(float(pv_kw), float(grid_kw), float(ess_kw))

    # Only non-None on the cycle right after a midnight rollover: archive the
    # finished day before composing the new day's report.
    stale = accumulator.take_stale_report()
    if stale is not None:
        try:
            history.archive(stale)
        except Exception as exc:
            LOG.warning("history archive of stale day failed: %s", exc)

    daily = compose_daily_report(cache, accumulator)
    accumulator.set_last_daily_report(daily)

    # Publish the composed daily report (native-first, balance-derived home load).
    put_path(cache, "plant.energy.daily.pv_kwh", daily["pv_kwh"])
    put_path(cache, "plant.energy.daily.grid_import_kwh", daily["grid_import_kwh"])
    put_path(cache, "plant.energy.daily.grid_export_kwh", daily["grid_export_kwh"])
    put_path(cache, "plant.energy.daily.home_load_kwh", daily["home_load_kwh"])
    put_path(cache, "plant.energy.daily.battery_charge_kwh", daily["battery_charge_kwh"])
    put_path(cache, "plant.energy.daily.battery_discharge_kwh", daily["battery_discharge_kwh"])
    put_path(cache, "plant.energy.daily.self_sufficiency_pct", daily["self_sufficiency_pct"])
    put_path(cache, "plant.energy.daily.self_consumption_pct", daily["self_consumption_pct"])
    put_path(cache, "plant.energy.daily.date", daily["date"])
    put_path(cache, "plant.energy.daily.coverage", daily["coverage"])

    # Period aggregates: today's live report plus archived history.
    try:
        history_days = history.load()
    except Exception as exc:
        LOG.warning("history load failed (%s); aggregating today only", exc)
        history_days = []
    try:
        periods = aggregate_periods(daily, history_days, daily["date"])
    except Exception as exc:
        LOG.warning("period aggregation failed: %s", exc)
        periods = {}
    put_path(cache, "plant.energy.periods", periods)

    # Cross-check block: integrated values for all six flows, kept unchanged.
    snap = accumulator.snapshot()
    put_path(cache, "plant.energy.daily.integrated", {
        "pv_kwh": snap["pv_kwh"],
        "grid_import_kwh": snap["grid_import_kwh"],
        "grid_export_kwh": snap["grid_export_kwh"],
        "home_load_kwh": snap["home_load_kwh"],
        "battery_charge_kwh": snap["battery_charge_kwh"],
        "battery_discharge_kwh": snap["battery_discharge_kwh"],
    })
    accumulator.persist()


# --- application --------------------------------------------------------------

class Application:
    """Wires components, owns the lifecycle event, runs the polling loop."""

    def __init__(self, cfg: dict[str, Any], once: bool) -> None:
        self.cfg = cfg
        self.once = once
        self.mb = cfg["modbus"]
        self.mc = cfg["mqtt"]
        self.tiers = cfg["polling"]
        self.stop_event = threading.Event()
        self.started_at = utc_now()
        self.fast_interval = float(self.tiers["fast"]["interval_seconds"])

    def _install_signal_handlers(self) -> None:
        def handler(signum: int, frame: Any) -> None:
            self.stop_event.set()
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def _build_energy_integration(self) -> tuple[EnergyAccumulator | None, DailyHistoryStore | None]:
        """Build the accumulator + history store, or (None, None) if disabled.
        On load, if the accumulator's persisted state belongs to a previous day,
        the stale daily report is archived into history before the accumulators
        reset for the new day."""
        ei = self.cfg.get("energy_integration") or {}
        if not ei.get("enabled"):
            return None, None
        tz_name = ei.get("timezone", "Europe/Warsaw")
        state_file = ei.get("state_file", "/var/lib/sigenergy-modbus-mqtt/energy_daily.json")
        default_history = str(Path(state_file).parent / "energy_history.json")
        history_file = ei.get("history_file", default_history)
        retention_days = int(ei.get("history_retention_days", 1100))
        accumulator = EnergyAccumulator(
            state_file=state_file,
            tz_name=tz_name,
            max_gap_s=ei.get("max_sample_gap_seconds", 30),
        )
        history = DailyHistoryStore(history_file, retention_days)
        stale = accumulator.load()
        if stale is not None:
            try:
                history.archive(stale)
            except Exception as exc:
                LOG.warning("history archive of stale day on load failed: %s", exc)
        return accumulator, history

    def run(self) -> int:
        self.publisher = MqttPublisher(self.mc)
        self.publisher.connect()
        self.modbus = ModbusTcpClient(
            self.mb["host"],
            port=int(self.mb.get("port", 502)),
            timeout=float(self.mb.get("timeout_seconds", 3)),
        )
        self.reader = RegisterReader(self.modbus, self.mb)
        self.poller = TierPoller(self.tiers, self.reader)
        self.accumulator, self.history = self._build_energy_integration()
        self._install_signal_handlers()

        try:
            while not self.stop_event.is_set():
                cycle = time.monotonic()
                self._run_cycle()
                if self.once:
                    break
                time.sleep(max(0.1, self.fast_interval - (time.monotonic() - cycle)))
        finally:
            self._shutdown()
        return 0

    def _run_cycle(self) -> None:
        try:
            if self.modbus.connected or self.modbus.connect():
                self._poll_and_publish()
            else:
                self._publish_offline()
        except Exception as exc:
            # transient Modbus/MQTT failure: never kill the daemon; force a clean
            # Modbus reconnect next cycle (paho reconnects MQTT on its own)
            LOG.error("cycle failed (%s); resetting connections and retrying", exc)
            self.reader.reset()

    def _poll_and_publish(self) -> None:
        self.poller.refresh_due(time.monotonic())
        age = self.poller.age_seconds(time.monotonic())
        payload = build_online_payload(
            self.mb, self.started_at, self.poller.tier_updated, age,
            self.poller.cache, self.poller.errors,
        )
        if self.accumulator is not None and self.history is not None:
            integrate_energy(self.poller.cache, self.accumulator, self.history)
        self.publisher.publish(payload)
        LOG.info(
            "Published consolidated snapshot to %s: %d errors",
            self.publisher.topic, len(self.poller.errors),
        )

    def _publish_offline(self) -> None:
        payload = build_offline_payload(self.mb, self.started_at, self.poller.tier_updated, self.poller.cache)
        self.publisher.publish(payload)
        LOG.error(payload["errors"]["connection"])

    def _shutdown(self) -> None:
        if self.accumulator is not None:
            try:
                self.accumulator.persist()
            except Exception as exc:
                LOG.warning("final energy persist failed: %s", exc)
        self.modbus.close()
        self.publisher.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    return Application(cfg, once=args.once).run()


if __name__ == "__main__":
    raise SystemExit(main())
