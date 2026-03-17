#!/usr/bin/env python3
"""
Backfill missed energy data from Home Assistant into LapaHub Cloud.

The HA addon's energy_report_loop was crashing due to a Python bug.
During that time, no energy data was pushed to the reportEnergy Cloud Function.
This script queries HA historical data and replays it to the cloud.

Usage:
    export HA_TOKEN="your_long_lived_access_token"
    export HUB_API_KEY="your_hub_api_key"

    # Backfill last 7 days
    python3 scripts/backfill_energy.py

    # Backfill specific range
    python3 scripts/backfill_energy.py --start 2026-03-08 --end 2026-03-15

    # Dry run
    python3 scripts/backfill_energy.py --dry-run --start 2026-03-10
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

try:
    import aiohttp
except ImportError:
    print("ERROR: aiohttp is required. Install with: pip install aiohttp")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backfill_energy")

# -------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------

HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")

HUB_ID = os.environ.get("HUB_ID", "hub_mjhil1ko_2e08cc35")
HUB_API_KEY = os.environ.get("HUB_API_KEY", "")

FIREBASE_PROJECT = os.environ.get("FIREBASE_PROJECT", "lapahub-dev-c8872")
CLOUD_FUNCTIONS_BASE = f"https://us-central1-{FIREBASE_PROJECT}.cloudfunctions.net"

# Sensors to query from HA history (power sensors report instantaneous W/kW)
SOLAR_SENSORS = [
    "sensor.sh25t_mppt1_energy",
    "sensor.sh25t_mppt2_energy",
    "sensor.sh25t_mppt3_energy",
]

GRID_IMPORT_SENSORS = [
    "sensor.sh25t_a23b1306258_total_purchased_energy",
]

BATTERY_DISCHARGE_SENSORS = [
    "sensor.sh25t_a23b1306258_battery_discharging_energy_today",
]

POWER_SENSORS = [
    "sensor.sh25t_a23b1306258_mppt_total_power",
    "sensor.sh25t_a23b1306258_total_load_active_power",
    "sensor.sh25t_a23b1306258_battery_discharging_power",
]

ALL_SENSORS = SOLAR_SENSORS + GRID_IMPORT_SENSORS + BATTERY_DISCHARGE_SENSORS + POWER_SENSORS

# Mapping of sensor -> dashboard source type
SENSOR_TYPE_MAP = {}
for s in SOLAR_SENSORS:
    SENSOR_TYPE_MAP[s] = "solar"
for s in GRID_IMPORT_SENSORS:
    SENSOR_TYPE_MAP[s] = "grid"
for s in BATTERY_DISCHARGE_SENSORS:
    SENSOR_TYPE_MAP[s] = "battery"
for s in POWER_SENSORS:
    SENSOR_TYPE_MAP[s] = "power"

# Rate limit: seconds between cloud function calls
REQUEST_DELAY = 1.0


# -------------------------------------------------------------------
# Home Assistant API helpers
# -------------------------------------------------------------------

async def ha_get(session: aiohttp.ClientSession, path: str, params: dict | None = None) -> dict | list | None:
    """Make a GET request to the Home Assistant REST API."""
    url = f"{HA_URL}{path}"
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                text = await resp.text()
                logger.error(f"HA API error {resp.status} for {path}: {text[:200]}")
                return None
    except Exception as e:
        logger.error(f"HA API request failed for {path}: {e}")
        return None


async def fetch_history_period(
    session: aiohttp.ClientSession,
    entity_ids: list[str],
    start: datetime,
    end: datetime,
) -> dict[str, list[dict]]:
    """
    Fetch history from HA for a list of entities over a time period.
    Uses /api/history/period/<timestamp> endpoint.

    Returns dict mapping entity_id -> list of state objects sorted by time.
    """
    # HA expects the start timestamp as part of the URL path
    start_iso = start.isoformat()
    end_iso = end.isoformat()

    filter_ids = ",".join(entity_ids)
    params = {
        "filter_entity_id": filter_ids,
        "end_time": end_iso,
        "minimal_response": "",
        "significant_changes_only": "0",
        "no_attributes": "",
    }

    data = await ha_get(session, f"/api/history/period/{start_iso}", params)
    if not data or not isinstance(data, list):
        return {}

    result: dict[str, list[dict]] = {}
    for entity_history in data:
        if not entity_history:
            continue
        entity_id = entity_history[0].get("entity_id", "")
        if entity_id:
            result[entity_id] = entity_history

    return result


async def fetch_current_states(session: aiohttp.ClientSession) -> dict[str, dict]:
    """Fetch current states to get attributes (unit, friendly_name, device_class)."""
    states = await ha_get(session, "/api/states")
    if not states:
        return {}
    lookup = {}
    for s in states:
        eid = s.get("entity_id", "")
        if eid:
            lookup[eid] = s
    return lookup


# -------------------------------------------------------------------
# Cloud Function helpers
# -------------------------------------------------------------------

async def authenticate_hub(session: aiohttp.ClientSession) -> str | None:
    """Authenticate with LapaHub cloud and return Bearer token."""
    url = f"{CLOUD_FUNCTIONS_BASE}/authenticateHub"
    try:
        async with session.post(
            url,
            json={"hubId": HUB_ID, "apiKey": HUB_API_KEY},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status == 200:
                creds = await resp.json()
                token = creds.get("token")
                logger.info("Successfully authenticated with LapaHub cloud")
                return token
            else:
                text = await resp.text()
                logger.error(f"Authentication failed ({resp.status}): {text[:200]}")
                return None
    except Exception as e:
        logger.error(f"Authentication request failed: {e}")
        return None


async def push_energy_report(
    session: aiohttp.ClientSession,
    token: str,
    payload: dict,
    dry_run: bool = False,
) -> bool:
    """POST energy data to the reportEnergy Cloud Function."""
    if dry_run:
        ts = payload.get("timestamp", "?")
        sensor_count = len(payload.get("sensors", {}))
        source_count = len(payload.get("dashboard_sources", {}))
        logger.info(f"[DRY RUN] Would send: timestamp={ts}, {sensor_count} sensors, {source_count} dashboard_sources")
        return True

    url = f"{CLOUD_FUNCTIONS_BASE}/reportEnergy"
    try:
        async with session.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status == 200:
                return True
            else:
                text = await resp.text()
                logger.warning(f"reportEnergy failed ({resp.status}): {text[:200]}")
                return False
    except Exception as e:
        logger.warning(f"reportEnergy request failed: {e}")
        return False


# -------------------------------------------------------------------
# History processing
# -------------------------------------------------------------------

def find_value_at_time(
    history: list[dict],
    target: datetime,
    tolerance_minutes: int = 30,
) -> float | None:
    """
    Find the sensor value closest to `target` from a history list.
    Each entry has 'last_changed' (or 'last_updated') and 's' (state) keys
    (minimal_response format).
    Returns None if no reading within tolerance.
    """
    best_value = None
    best_delta = timedelta(minutes=tolerance_minutes + 1)

    for entry in history:
        # minimal_response format uses 'lu' for last_updated timestamp
        ts_str = entry.get("last_changed") or entry.get("lu") or entry.get("last_updated")
        if not ts_str:
            continue

        try:
            # Handle both full datetime and epoch formats
            if isinstance(ts_str, (int, float)):
                entry_time = datetime.fromtimestamp(ts_str, tz=timezone.utc)
            else:
                # HA returns ISO format, sometimes with +00:00, sometimes with Z
                ts_str = ts_str.replace("Z", "+00:00")
                entry_time = datetime.fromisoformat(ts_str)
                if entry_time.tzinfo is None:
                    entry_time = entry_time.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue

        # State value: minimal_response uses 's' key, full response uses 'state'
        state_val = entry.get("s") or entry.get("state", "")
        if state_val in ("unknown", "unavailable", ""):
            continue

        try:
            value = float(state_val)
        except (ValueError, TypeError):
            continue

        delta = abs(entry_time - target)
        if delta < best_delta:
            best_delta = delta
            best_value = value

    if best_delta <= timedelta(minutes=tolerance_minutes):
        return best_value
    return None


def build_hourly_payloads(
    history: dict[str, list[dict]],
    state_attrs: dict[str, dict],
    start: datetime,
    end: datetime,
) -> list[dict]:
    """
    Build one reportEnergy payload per hour in the [start, end) range.
    For each hour, pick the closest sensor reading to the hour mark.
    """
    payloads = []
    current = start.replace(minute=0, second=0, microsecond=0)

    while current < end:
        sensors = {}
        dashboard_sources = {}

        for entity_id in ALL_SENSORS:
            entity_history = history.get(entity_id, [])
            value = find_value_at_time(entity_history, current)

            if value is None:
                continue

            attrs = state_attrs.get(entity_id, {}).get("attributes", {})
            unit = attrs.get("unit_of_measurement", "")
            device_class = attrs.get("device_class", "")
            friendly_name = attrs.get("friendly_name", entity_id)

            # Add to sensors dict (all energy-class sensors)
            sensors[entity_id] = {
                "value": value,
                "unit": unit,
                "device_class": device_class,
                "friendly_name": friendly_name,
            }

            # Add to dashboard_sources with the appropriate type
            source_type = SENSOR_TYPE_MAP.get(entity_id)
            if source_type:
                dashboard_sources[entity_id] = {
                    "type": source_type,
                    "value": value,
                    "unit": unit,
                    "friendly_name": friendly_name,
                }

        if sensors:
            payloads.append({
                "hubId": HUB_ID,
                "timestamp": current.isoformat(),
                "sensors": sensors,
                "dashboard_sources": dashboard_sources,
            })

        current += timedelta(hours=1)

    return payloads


# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------

async def main(args: argparse.Namespace):
    # Validate required env vars
    if not HA_TOKEN:
        logger.error("HA_TOKEN environment variable is required")
        sys.exit(1)
    if not args.dry_run and not HUB_API_KEY:
        logger.error("HUB_API_KEY environment variable is required (or use --dry-run)")
        sys.exit(1)

    # Determine date range
    if args.end:
        end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        end_dt = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    if args.start:
        start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        start_dt = end_dt - timedelta(days=7)

    if start_dt >= end_dt:
        logger.error(f"Start ({start_dt.date()}) must be before end ({end_dt.date()})")
        sys.exit(1)

    total_hours = int((end_dt - start_dt).total_seconds() / 3600)
    logger.info(f"Backfill range: {start_dt.date()} to {end_dt.date()} ({total_hours} hours)")
    logger.info(f"Hub ID: {HUB_ID}")
    logger.info(f"HA URL: {HA_URL}")
    logger.info(f"Sensors: {len(ALL_SENSORS)}")
    logger.info(f"Dry run: {args.dry_run}")

    async with aiohttp.ClientSession() as session:
        # Step 1: Fetch current states for attribute info (unit, friendly_name, etc.)
        logger.info("Fetching current sensor attributes from HA...")
        state_attrs = await fetch_current_states(session)
        found = [s for s in ALL_SENSORS if s in state_attrs]
        logger.info(f"Found {len(found)}/{len(ALL_SENSORS)} sensors in HA")
        if not found:
            logger.error("No target sensors found in HA. Check sensor names and HA connection.")
            sys.exit(1)

        for s in ALL_SENSORS:
            if s not in state_attrs:
                logger.warning(f"Sensor not found in HA: {s}")

        # Step 2: Fetch historical data
        # HA /api/history/period can handle large ranges but we chunk by day
        # to avoid timeouts and excessive memory usage.
        logger.info("Fetching historical data from HA...")
        all_history: dict[str, list[dict]] = {}

        chunk_start = start_dt
        day_count = 0
        while chunk_start < end_dt:
            chunk_end = min(chunk_start + timedelta(days=1), end_dt)
            day_count += 1
            logger.info(f"  Fetching day {day_count}: {chunk_start.date()}")

            chunk_history = await fetch_history_period(session, ALL_SENSORS, chunk_start, chunk_end)
            for entity_id, entries in chunk_history.items():
                if entity_id not in all_history:
                    all_history[entity_id] = []
                all_history[entity_id].extend(entries)

            chunk_start = chunk_end

        entities_with_data = [e for e in ALL_SENSORS if all_history.get(e)]
        total_points = sum(len(v) for v in all_history.values())
        logger.info(f"Retrieved history for {len(entities_with_data)} sensors ({total_points} data points)")

        if not entities_with_data:
            logger.error("No historical data found. The period may be too old or sensors were unavailable.")
            sys.exit(1)

        # Step 3: Build hourly payloads
        logger.info("Building hourly energy payloads...")
        payloads = build_hourly_payloads(all_history, state_attrs, start_dt, end_dt)
        logger.info(f"Built {len(payloads)} payloads ({len(payloads)}/{total_hours} hours have data)")

        if not payloads:
            logger.warning("No payloads to send. Historical data may not overlap with the requested period.")
            return

        # Step 4: Authenticate with cloud (unless dry run)
        token = None
        if not args.dry_run:
            logger.info("Authenticating with LapaHub cloud...")
            token = await authenticate_hub(session)
            if not token:
                logger.error("Failed to authenticate. Check HUB_ID and HUB_API_KEY.")
                sys.exit(1)

        # Step 5: Send payloads with rate limiting
        success_count = 0
        fail_count = 0
        for i, payload in enumerate(payloads, 1):
            ts = payload["timestamp"]
            sensor_count = len(payload["sensors"])
            source_count = len(payload["dashboard_sources"])

            logger.info(f"[{i}/{len(payloads)}] Sending {ts} ({sensor_count} sensors, {source_count} sources)")

            ok = await push_energy_report(session, token or "", payload, dry_run=args.dry_run)
            if ok:
                success_count += 1
            else:
                fail_count += 1

            # Rate limit between requests
            if i < len(payloads):
                await asyncio.sleep(REQUEST_DELAY)

        # Summary
        logger.info("=" * 60)
        logger.info("Backfill complete!")
        logger.info(f"  Period: {start_dt.date()} to {end_dt.date()}")
        logger.info(f"  Total payloads: {len(payloads)}")
        logger.info(f"  Successful: {success_count}")
        logger.info(f"  Failed: {fail_count}")
        if args.dry_run:
            logger.info("  (DRY RUN - nothing was actually sent)")
        logger.info("=" * 60)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill missed energy data from Home Assistant to LapaHub Cloud",
    )
    parser.add_argument(
        "--start",
        type=str,
        default=None,
        help="Start date in ISO format (YYYY-MM-DD). Default: 7 days ago.",
    )
    parser.add_argument(
        "--end",
        type=str,
        default=None,
        help="End date in ISO format (YYYY-MM-DD). Default: now.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print what would be sent without actually sending.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args))
