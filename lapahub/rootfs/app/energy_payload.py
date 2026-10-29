"""Energy snapshots from a successful current HA states fetch, never estimates."""
import math
from datetime import datetime, timezone


class EnergyDeliveryError(RuntimeError):
    def __init__(self, status):
        super().__init__(f"Energy push failed: HTTP {status}")
        self.permanent = 400 <= status < 500 and status not in (408, 429)


def numeric(value):
    """Unknown states and nonfinite/boolean values are missing, never zero."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def power_observation(state, fetched_at):
    """A current HA snapshot alone does not prove a cached power report fresh.

    Ten minutes matches the cloud observation window and two default addon
    reporting cycles. Missing report clocks (including older HA) mean unknown
    freshness, never a hardware-fault diagnosis. last_updated is unsuitable:
    legitimate unchanged MQTT readings can have an old change timestamp.
    """
    reported = state.get("last_reported")
    if not isinstance(reported, str):
        return None
    try:
        timestamp = datetime.fromisoformat(reported.replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            return None
        age = (fetched_at - timestamp).total_seconds()
        return timestamp if -60 <= age <= 600 else None
    except (ValueError, TypeError, OverflowError):
        return None


def collect_energy_payload(states, preferences, fetched_at=None):
    # Top-level time describes the HA fetch; power observations retain the
    # actual source report clock instead of re-stamping cached values.
    timestamp = fetched_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Energy observation time requires a timezone")
    payload = {"energy_payload_version": 2, "timestamp": timestamp.isoformat(),
               "sensors": {}, "dashboard_sources": {}}
    lookup = {}
    for state in states:
        entity = state.get("entity_id", "")
        if not entity.startswith("sensor."):
            continue
        lookup[entity] = lookup[entity[7:]] = state
        attributes = state.get("attributes") or {}
        unit = attributes.get("unit_of_measurement")
        device_class = attributes.get("device_class")
        if not ((device_class == "power" and unit in ("W", "kW", "MW")) or
                (device_class == "battery" and unit == "%")):
            continue
        if len(payload["sensors"]) >= 512:
            raise ValueError("Energy snapshot exceeds 512 supported sensors")
        value = numeric(state.get("state"))
        observed = power_observation(state, timestamp) if device_class == "power" else timestamp
        if observed is None:
            value = None
        if unit == "%" and value is not None and not 0 <= value <= 100:
            value = None
        payload["sensors"][entity] = {
            "value": value, "unit": unit,
            "device_class": device_class,
            "friendly_name": attributes.get("friendly_name"),
            "observed_at": (observed or timestamp).isoformat(),
            "state_updated_at": state.get("last_updated"),
        }

    def add(stat_id, source_type, subtype=None, power=False):
        if not isinstance(stat_id, str):
            return
        state = lookup.get(stat_id.removeprefix("recorder:"))
        if state is None:
            return
        attrs = state.get("attributes") or {}
        unit = attrs.get("unit_of_measurement")
        value = numeric(state.get("state"))
        if not isinstance(unit, str) or not unit or value is None:
            return
        # Every dashboard power path needs the same qualification, including
        # legacy source fields and sources without device_class metadata.
        observed = power_observation(state, timestamp) if unit in ("W", "kW", "MW") else timestamp
        if observed is None:
            return
        entry = {"type": source_type, "value": value, "unit": unit,
                 "friendly_name": attrs.get("friendly_name"),
                 "observed_at": observed.isoformat(),
                 "is_power_sensor": power and unit in ("W", "kW", "MW")}
        if subtype:
            entry["subtype"] = subtype
        payload["dashboard_sources"][stat_id] = entry

    for source in preferences.get("energy_sources", []):
        kind = source.get("type")
        power = source.get("power") or []
        if isinstance(power, str):
            add(power, kind, power=True)
        elif isinstance(power, list):
            for item in power:
                if isinstance(item, dict):
                    add(item.get("stat_rate"), kind, power=True)
        if kind == "grid":
            for flow in source.get("flow_from", []):
                add(flow.get("stat_energy_from"), kind, "import")
            for flow in source.get("flow_to", []):
                add(flow.get("stat_energy_to"), kind, "export")
        else:
            add(source.get("stat_energy_from") or source.get("stat_energy_to") or
                source.get("entity_id") or source.get("stat_compensation"), kind)
    for device in preferences.get("device_consumption", []):
        add(device.get("stat_consumption"), "device")
    return payload
