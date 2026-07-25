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
from datetime import datetime, timezone
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
        for f in self.FIELDS:
            setattr(self, f, 0.0)

    def _local_date(self) -> str:
        return datetime.now(self.tz).date().isoformat()

    def load(self) -> None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:
            LOG.warning("energy state file %s unreadable (%s); starting fresh", self.state_file, exc)
            return
        today = self._local_date()
        self.date = data.get("date") or today
        if self.date != today:
            # stored day has passed -> start the new day at zero
            self.date = today
            for f in self.FIELDS:
                setattr(self, f, 0.0)
            LOG.info("energy state dated %s != today %s; reset accumulators", data.get("date"), today)
            return
        for f in self.FIELDS:
            try:
                setattr(self, f, float(data.get(f, 0.0)))
            except (TypeError, ValueError):
                setattr(self, f, 0.0)
        LOG.info("energy state loaded for %s: %s", self.date,
                 {f: round(getattr(self, f), 3) for f in self.FIELDS})

    def update(self, pv_kw: float, grid_kw: float, ess_kw: float) -> None:
        today = self._local_date()
        if self.date != today:
            if self.date is not None:
                LOG.info("local date %s -> %s; resetting daily energy accumulators", self.date, today)
            self.date = today
            for f in self.FIELDS:
                setattr(self, f, 0.0)
        now = time.monotonic()
        if self.last_ts is None:
            self.last_ts = now
            return
        dt = now - self.last_ts
        self.last_ts = now
        if dt <= 0:
            return
        if dt > self.max_gap_s:
            # gap too large (stalled loop or restart) -> unmeasured; do not backfill
            LOG.debug("skipping integration sample: dt=%.1fs exceeds max_gap_s=%.0f", dt, self.max_gap_s)
            return
        h = dt / 3600.0
        # grid: >0 import, <0 export (per doc 30005)
        if grid_kw > 0:
            self.grid_import_kwh += grid_kw * h
        elif grid_kw < 0:
            self.grid_export_kwh += (-grid_kw) * h
        # pv: >=0 generation
        if pv_kw > 0:
            self.pv_kwh += pv_kw * h
        # ess: >0 charging (house->battery), <0 discharging (battery->house) (per doc 30037)
        if ess_kw > 0:
            self.battery_charge_kwh += ess_kw * h
        elif ess_kw < 0:
            self.battery_discharge_kwh += (-ess_kw) * h
        # home load = pv + grid_import_signed - ess_charge_signed (clamp >= 0)
        home = pv_kw + grid_kw - ess_kw
        if home > 0:
            self.home_load_kwh += home * h

    def snapshot(self) -> dict[str, float]:
        return {f: round(getattr(self, f), 3) for f in self.FIELDS}

    def persist(self) -> None:
        snap = self.snapshot()
        snap["date"] = self.date
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
            interval = float(tier["interval_seconds"])
            if self.tier_last[tier_name] and now - self.tier_last[tier_name] < interval:
                continue
            success = 0
            for reg in tier.get("registers", []):
                if reg.get("enabled", True) is False:
                    continue
                path = reg["path"]
                try:
                    put_path(self.cache, path, self.reader.read(reg))
                    self.errors.pop(path, None)
                    success += 1
                except Exception as exc:
                    self.errors[path] = str(exc)
                    LOG.warning("%s: %s", path, exc)
            self.tier_last[tier_name] = now
            self.tier_updated[tier_name] = utc_now()
            LOG.info("Refreshed %s tier: %d values", tier_name, success)

    def age_seconds(self, now: float) -> dict[str, Any]:
        return {
            name: (None if self.tier_last[name] == 0 else round(now - self.tier_last[name], 1))
            for name in self.tiers
        }


# --- payload builders ---------------------------------------------------------

def build_online_payload(
    mb: dict[str, Any],
    started_at: str,
    tier_updated: dict[str, Any],
    age: dict[str, Any],
    cache: dict[str, Any],
    errors: dict[str, str],
) -> dict[str, Any]:
    return {
        "timestamp": utc_now(),
        "online": True,
        "device": {
            "host": mb["host"],
            "port": int(mb.get("port", 502)),
            "inverter_unit_id": int(mb.get("inverter_unit_id", 1)),
        },
        "started_at": started_at,
        "updated_at": tier_updated,
        "age_seconds": age,
        "data": cache,
        "errors": errors,
    }


def build_offline_payload(
    mb: dict[str, Any],
    started_at: str,
    tier_updated: dict[str, Any],
    cache: dict[str, Any],
) -> dict[str, Any]:
    return {
        "timestamp": utc_now(),
        "online": False,
        "device": {
            "host": mb["host"],
            "port": int(mb.get("port", 502)),
        },
        "started_at": started_at,
        "errors": {
            "connection": f"Cannot connect to Modbus TCP {mb['host']}:{mb.get('port', 502)}"
        },
        "updated_at": tier_updated,
        "data": cache,
    }


# --- energy integration step --------------------------------------------------

def integrate_energy(cache: dict[str, Any], accumulator: EnergyAccumulator) -> None:
    """Power-integration step run each cycle when accumulator is enabled."""
    pv_kw = get_path(cache, "plant.pv.power_kw")
    grid_kw = get_path(cache, "plant.grid_sensor.active_power_kw")
    ess_kw = get_path(cache, "plant.ess.power_kw")
    if not all(isinstance(v, (int, float)) for v in (pv_kw, grid_kw, ess_kw)):
        return
    accumulator.update(float(pv_kw), float(grid_kw), float(ess_kw))
    snap = accumulator.snapshot()
    # consolidated 6-flow daily Sankey (native where available, integrated for grid/home)
    put_path(cache, "plant.energy.daily.grid_import_kwh", snap["grid_import_kwh"])
    put_path(cache, "plant.energy.daily.grid_export_kwh", snap["grid_export_kwh"])
    put_path(cache, "plant.energy.daily.home_load_kwh", snap["home_load_kwh"])
    bc = get_path(cache, "inverter.energy.daily.battery_charge_kwh")
    bd = get_path(cache, "inverter.energy.daily.battery_discharge_kwh")
    if isinstance(bc, (int, float)):
        put_path(cache, "plant.energy.daily.battery_charge_kwh", round(float(bc), 3))
    if isinstance(bd, (int, float)):
        put_path(cache, "plant.energy.daily.battery_discharge_kwh", round(float(bd), 3))
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

    def run(self) -> int:
        publisher = MqttPublisher(self.mc)
        publisher.connect()

        modbus = ModbusTcpClient(
            self.mb["host"],
            port=int(self.mb.get("port", 502)),
            timeout=float(self.mb.get("timeout_seconds", 3)),
        )
        reader = RegisterReader(modbus, self.mb)
        poller = TierPoller(self.tiers, reader)

        ei = self.cfg.get("energy_integration") or {}
        accumulator: EnergyAccumulator | None = None
        if ei.get("enabled"):
            accumulator = EnergyAccumulator(
                state_file=ei.get("state_file", "/var/lib/sigenergy-modbus-mqtt/energy_daily.json"),
                tz_name=ei.get("timezone", "Europe/Warsaw"),
                max_gap_s=ei.get("max_sample_gap_seconds", 30),
            )
            accumulator.load()

        self._install_signal_handlers()

        try:
            while not self.stop_event.is_set():
                cycle = time.monotonic()
                try:
                    if not modbus.connected and not modbus.connect():
                        payload = build_offline_payload(
                            self.mb, self.started_at, poller.tier_updated, poller.cache
                        )
                        publisher.publish(payload)
                        LOG.error(payload["errors"]["connection"])
                    else:
                        now = time.monotonic()
                        poller.refresh_due(now)
                        age = poller.age_seconds(time.monotonic())
                        payload = build_online_payload(
                            self.mb, self.started_at, poller.tier_updated, age, poller.cache, poller.errors
                        )
                        if accumulator is not None:
                            integrate_energy(poller.cache, accumulator)
                        publisher.publish(payload)
                        LOG.info(
                            "Published consolidated snapshot to %s: %d errors",
                            publisher.topic, len(poller.errors),
                        )
                except Exception as exc:
                    # transient Modbus/MQTT failure: never kill the daemon; force a clean
                    # Modbus reconnect next cycle (paho reconnects MQTT on its own)
                    LOG.error("cycle failed (%s); resetting connections and retrying", exc)
                    try:
                        reader.reset()
                    except Exception:
                        pass
                if self.once:
                    break
                time.sleep(max(0.1, self.fast_interval - (time.monotonic() - cycle)))
        finally:
            if accumulator is not None:
                try:
                    accumulator.persist()
                except Exception as exc:
                    LOG.warning("final energy persist failed: %s", exc)
            modbus.close()
            publisher.close()
        return 0


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
