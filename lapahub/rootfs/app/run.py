#!/usr/bin/env python3
"""
LapaHub Home Assistant Addon

Connects Home Assistant to LapaHub cloud for smart energy management.
- Syncs devices to Firebase via WebSocket real-time updates
- Receives commands from cloud
- Reports energy data
- Syncs scenes and automations
"""

import asyncio
import json
import logging
import os
import signal
import sys
import psutil
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import deque

import aiohttp
from aiohttp import web

# Configure logging
LOG_LEVEL = os.environ.get("LOG_LEVEL", "info").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("lapahub")

# Configuration
HA_BASE_URL = "http://supervisor/core"
OPTIONS_PATH = Path("/data/options.json")
SUPERVISOR_TOKEN_PATH = Path("/run/supervisor/token")
ADDON_VERSION = "1.0.26"  # Keep in sync with config.yaml


def get_supervisor_token() -> str | None:
    """Get supervisor token from environment or file."""
    # Try environment variable first
    token = os.environ.get("SUPERVISOR_TOKEN")
    if token:
        logger.debug("Got SUPERVISOR_TOKEN from environment")
        return token

    # Try HASSIO_TOKEN (older name)
    token = os.environ.get("HASSIO_TOKEN")
    if token:
        logger.debug("Got HASSIO_TOKEN from environment")
        return token

    # Try reading from standard supervisor token file
    if SUPERVISOR_TOKEN_PATH.exists():
        try:
            token = SUPERVISOR_TOKEN_PATH.read_text().strip()
            if token:
                logger.debug("Got token from /run/supervisor/token")
                return token
        except Exception as e:
            logger.warning(f"Failed to read token file: {e}")

    # Try s6-overlay container environment directory
    s6_env_path = Path("/run/s6/container_environment/SUPERVISOR_TOKEN")
    if s6_env_path.exists():
        try:
            token = s6_env_path.read_text().strip()
            if token:
                logger.debug("Got token from s6 container environment")
                return token
        except Exception:
            pass

    # Try /var/run/s6/container_environment
    alt_s6_path = Path("/var/run/s6/container_environment/SUPERVISOR_TOKEN")
    if alt_s6_path.exists():
        try:
            token = alt_s6_path.read_text().strip()
            if token:
                logger.debug("Got token from /var/run/s6 container environment")
                return token
        except Exception:
            pass

    logger.warning("No supervisor token found - HA API calls will fail")
    return None


SUPERVISOR_TOKEN = get_supervisor_token()
if SUPERVISOR_TOKEN:
    logger.info(f"Supervisor token obtained (length: {len(SUPERVISOR_TOKEN)})")
else:
    logger.error("No supervisor token - Home Assistant API calls will fail!")


class LapaHubAddon:
    """Main addon class that manages all LapaHub functionality."""

    def __init__(self):
        self.options = self._load_options()
        self.hub_id = self.options.get("hub_id", "")
        self.api_key = self.options.get("api_key", "")
        self.sync_interval = self.options.get("sync_interval_seconds", 60)
        self.energy_interval = self.options.get("energy_report_interval_seconds", 300)
        self.firebase_project = os.environ.get("FIREBASE_PROJECT_ID", "lapahub-dev-c8872")

        # Sensor realtime config (BETA: default off to reduce Firestore writes)
        self.sensor_realtime = self.options.get("sensor_realtime", False)
        self.sensor_poll_interval = self.options.get("sensor_poll_interval_seconds", 30)

        self.session: aiohttp.ClientSession | None = None
        self.devices: dict = {}
        self.running = True
        self.shutting_down = False

        # Firebase credentials from API key exchange
        self.firebase_credentials = None
        self.token_expires_at: datetime | None = None
        self.token_refresh_margin = timedelta(minutes=5)  # Refresh 5 min before expiry

        # Reconnection settings
        self.max_retries = 10
        self.base_retry_delay = 5  # seconds
        self.max_retry_delay = 300  # 5 minutes max
        self.current_retry_count = 0

        # Web server runner for graceful shutdown
        self.web_runner: web.AppRunner | None = None

        # WebSocket connection for real-time updates (LAPA-74)
        self.ha_websocket: aiohttp.ClientWebSocketResponse | None = None
        self.ws_message_id = 0

        # Health metrics (LAPA-75)
        self.start_time = datetime.now(timezone.utc)
        self.heartbeat_interval = 60  # seconds
        self.last_device_sync: datetime | None = None
        self.last_energy_report: datetime | None = None
        self.sync_error_count = 0
        self.energy_error_count = 0

        # Activity log for web UI (LAPA-77)
        self.activity_log: deque = deque(maxlen=50)

        # Scenes and automations (LAPA-78)
        self.scenes: dict = {}
        self.automations: dict = {}

        # Energy Dashboard configuration from HA
        self.energy_prefs: dict | None = None
        self.energy_prefs_last_fetch: datetime | None = None

        # Version info for remote tracking
        self.addon_version = ADDON_VERSION
        self.ha_version: str | None = None

    def _load_options(self) -> dict:
        """Load addon options from config."""
        if OPTIONS_PATH.exists():
            with open(OPTIONS_PATH) as f:
                return json.load(f)
        return {}

    async def fetch_ha_version(self):
        """Fetch Home Assistant version from supervisor API."""
        try:
            async with self.session.get(
                "http://supervisor/core/info",
                headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.ha_version = data.get("data", {}).get("version", "unknown")
                    logger.info(f"Home Assistant version: {self.ha_version}")
                else:
                    logger.warning(f"Could not fetch HA version: {resp.status}")
        except Exception as e:
            logger.warning(f"Error fetching HA version: {e}")

    @property
    def version_string(self) -> str:
        """Get combined version string (addon:HA)."""
        ha_ver = self.ha_version or "unknown"
        return f"{self.addon_version}:{ha_ver}"

    def log_activity(self, message: str, level: str = "info"):
        """Log activity for web UI display."""
        self.activity_log.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": message,
            "level": level,
        })
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)

    async def start(self):
        """Start the addon."""
        logger.info(f"Starting LapaHub Addon v{self.addon_version}")
        logger.info(f"Realtime mode: commands=always, binary_sensors=always, sensors={'realtime' if self.sensor_realtime else f'polling ({self.sensor_poll_interval}s)'}")

        # Set up signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self.shutdown(s)))

        if not self.hub_id or not self.api_key:
            logger.error("Hub ID and API Key must be configured in addon options")
            return

        # Create aiohttp session
        self.session = aiohttp.ClientSession()

        try:
            # Fetch HA version for remote tracking
            await self.fetch_ha_version()

            # Authenticate with LapaHub cloud (with retries)
            await self.authenticate_with_retry()

            # Start background tasks
            self.log_activity("Starting background tasks")
            tasks = [
                self.device_sync_loop(),
                self.command_listener_loop(),
                self.energy_report_loop(),
                self.token_refresh_loop(),
                self.ha_websocket_loop(),      # LAPA-74: Real-time state updates
                self.heartbeat_loop(),          # LAPA-75: Health heartbeat
                self.scene_sync_loop(),         # LAPA-78: Sync scenes/automations
                self.run_web_server(),
            ]
            # Add sensor polling loop only if sensors are not realtime
            if not self.sensor_realtime:
                tasks.append(self.sensor_poll_loop())
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Tasks cancelled during shutdown")
        except Exception as e:
            logger.exception(f"Fatal error: {e}")
        finally:
            await self.cleanup()

    async def shutdown(self, sig):
        """Handle graceful shutdown."""
        if self.shutting_down:
            return
        self.shutting_down = True
        self.running = False

        logger.info(f"Received signal {sig.name}, initiating graceful shutdown...")

        # Cancel all running tasks except the current one
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        logger.info(f"Cancelling {len(tasks)} outstanding tasks...")

        for task in tasks:
            task.cancel()

        # Wait for tasks to complete with timeout
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("All tasks cancelled")

    async def cleanup(self):
        """Clean up resources."""
        self.log_activity("Cleaning up resources...")

        # Close WebSocket connection
        if self.ha_websocket and not self.ha_websocket.closed:
            await self.ha_websocket.close()
            logger.info("Home Assistant WebSocket closed")

        # Close web server
        if self.web_runner:
            await self.web_runner.cleanup()
            logger.info("Web server stopped")

        # Close HTTP session
        if self.session:
            await self.session.close()
            logger.info("HTTP session closed")

        logger.info("Cleanup complete, addon stopped")

    async def authenticate_with_retry(self):
        """Authenticate with retries using exponential backoff."""
        while self.running and self.current_retry_count < self.max_retries:
            success = await self.authenticate()
            if success:
                self.current_retry_count = 0  # Reset on success
                return True

            self.current_retry_count += 1
            if self.current_retry_count >= self.max_retries:
                logger.error(f"Authentication failed after {self.max_retries} attempts")
                logger.warning("Continuing in offline mode")
                return False

            # Calculate delay with exponential backoff
            delay = min(
                self.base_retry_delay * (2 ** (self.current_retry_count - 1)),
                self.max_retry_delay
            )
            logger.info(f"Retrying authentication in {delay}s (attempt {self.current_retry_count}/{self.max_retries})")
            await asyncio.sleep(delay)

        return False

    async def authenticate(self) -> bool:
        """Authenticate with LapaHub cloud and get Firebase credentials."""
        logger.info(f"Authenticating hub {self.hub_id} with LapaHub cloud...")

        api_url = f"https://us-central1-{self.firebase_project}.cloudfunctions.net/authenticateHub"

        try:
            async with self.session.post(
                api_url,
                json={"hubId": self.hub_id, "apiKey": self.api_key},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    self.firebase_credentials = await resp.json()
                    # Track token expiry (default 24 hours if not provided)
                    expires_in = self.firebase_credentials.get("expiresIn", 86400)
                    self.token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
                    logger.info(f"Successfully authenticated (token expires at {self.token_expires_at.isoformat()})")
                    return True
                else:
                    error = await resp.text()
                    logger.error(f"Authentication failed ({resp.status}): {error}")
                    return False
        except aiohttp.ClientError as e:
            logger.warning(f"Could not reach LapaHub cloud: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected authentication error: {e}")
            return False

    async def token_refresh_loop(self):
        """Periodically check and refresh token before expiry."""
        logger.info("Starting token refresh loop")

        while self.running:
            try:
                if self.token_expires_at:
                    time_until_expiry = self.token_expires_at - datetime.now(timezone.utc)

                    # Refresh if within margin of expiry
                    if time_until_expiry <= self.token_refresh_margin:
                        logger.info("Token expiring soon, refreshing...")
                        success = await self.authenticate()
                        if not success:
                            # Try with retries if direct refresh fails
                            await self.authenticate_with_retry()
                elif not self.firebase_credentials:
                    # No credentials, try to authenticate
                    logger.info("No valid credentials, attempting authentication...")
                    await self.authenticate_with_retry()

            except Exception as e:
                logger.error(f"Error in token refresh loop: {e}")

            # Check every minute
            await asyncio.sleep(60)

    # ==================== LAPA-74: WebSocket Real-time Updates ====================

    async def ha_websocket_loop(self):
        """Maintain WebSocket connection to Home Assistant for real-time updates."""
        logger.info("Starting Home Assistant WebSocket loop")

        while self.running:
            try:
                await self.connect_ha_websocket()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log_activity(f"WebSocket error: {e}", "error")
                await asyncio.sleep(10)  # Wait before reconnecting

    async def connect_ha_websocket(self):
        """Connect to Home Assistant WebSocket API."""
        ws_url = "ws://supervisor/core/websocket"

        try:
            self.ha_websocket = await self.session.ws_connect(ws_url)
            self.log_activity("Connected to Home Assistant WebSocket")

            # Authenticate
            auth_msg = await self.ha_websocket.receive_json()
            if auth_msg.get("type") == "auth_required":
                await self.ha_websocket.send_json({
                    "type": "auth",
                    "access_token": SUPERVISOR_TOKEN,
                })
                auth_result = await self.ha_websocket.receive_json()
                if auth_result.get("type") != "auth_ok":
                    raise Exception(f"WebSocket auth failed: {auth_result}")
                self.log_activity("WebSocket authenticated")

            # Fetch Energy Dashboard preferences on connect
            await self.fetch_energy_prefs()

            # Subscribe to state changes
            self.ws_message_id += 1
            await self.ha_websocket.send_json({
                "id": self.ws_message_id,
                "type": "subscribe_events",
                "event_type": "state_changed",
            })

            # Subscribe to HA start event (to refresh energy prefs on HA restart)
            self.ws_message_id += 1
            await self.ha_websocket.send_json({
                "id": self.ws_message_id,
                "type": "subscribe_events",
                "event_type": "homeassistant_started",
            })

            # Listen for messages
            async for msg in self.ha_websocket:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    await self.handle_ws_message(data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

        except Exception as e:
            self.log_activity(f"WebSocket connection error: {e}", "warning")
        finally:
            if self.ha_websocket and not self.ha_websocket.closed:
                await self.ha_websocket.close()
            self.ha_websocket = None

    async def handle_ws_message(self, data: dict):
        """Handle incoming WebSocket message from Home Assistant."""
        msg_type = data.get("type")

        if msg_type == "event":
            event = data.get("event", {})
            event_type = event.get("event_type")

            if event_type == "state_changed":
                await self.handle_state_change(event.get("data", {}))
            elif event_type == "homeassistant_started":
                # HA restarted - refresh energy dashboard config
                logger.info("Home Assistant started - refreshing Energy Dashboard config")
                self.log_activity("HA restarted - refreshing config")
                await self.fetch_energy_prefs()

    async def handle_state_change(self, data: dict):
        """Handle a state change event - push to cloud based on realtime config."""
        import time
        start_time = time.time()

        entity_id = data.get("entity_id", "")
        new_state = data.get("new_state")

        if not new_state or not entity_id:
            return

        # Filter to relevant domains
        domain = entity_id.split(".")[0] if "." in entity_id else ""
        relevant_domains = [
            "switch", "light", "climate", "cover", "fan",
            "sensor", "binary_sensor", "lock", "media_player",
            "scene", "automation",
        ]

        if domain not in relevant_domains:
            return

        new_state_value = new_state.get("state")

        # Realtime filtering based on config:
        # - Commands (light, switch, cover, climate, fan, lock, media_player): Always realtime
        # - Binary sensors: Always realtime (security relevant - motion, doors, etc.)
        # - Sensors: Only realtime if sensor_realtime is True, otherwise polled
        realtime_domains = [
            "switch", "light", "climate", "cover", "fan",
            "lock", "media_player", "scene", "automation",
            "binary_sensor",  # Always realtime for security
        ]

        if domain == "sensor" and not self.sensor_realtime:
            # Skip realtime push for sensors when in polling mode
            # Just update local cache, sensor_poll_loop will handle cloud sync
            logger.debug(f"[POLL] Sensor change cached: {entity_id} -> {new_state_value}")
        else:
            logger.info(f"[RT] State change: {entity_id} -> {new_state_value}")

        # Update local cache
        attributes = new_state.get("attributes", {})
        device = {
            "entity_id": entity_id,
            "domain": domain,
            "friendly_name": attributes.get("friendly_name", entity_id),
            "device_class": attributes.get("device_class"),
            "state": new_state_value,
            "attributes": attributes,
            "last_updated": new_state.get("last_updated"),
        }
        self.devices[entity_id] = device

        # Push state change to cloud (skip sensors in polling mode)
        should_push_realtime = domain != "sensor" or self.sensor_realtime
        if self.firebase_credentials and should_push_realtime:
            await self.push_state_change_to_cloud(device, start_time)

    async def push_state_change_to_cloud(self, device: dict, start_time: float = None):
        """Push a single device state change to cloud."""
        import time
        api_url = f"https://us-central1-{self.firebase_project}.cloudfunctions.net/updateDeviceState"

        try:
            async with self.session.post(
                api_url,
                json={
                    "hubId": self.hub_id,
                    "device": device,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                headers={"Authorization": f"Bearer {self.firebase_credentials.get('token', '')}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                elapsed = (time.time() - start_time) * 1000 if start_time else 0
                if resp.status == 200:
                    logger.info(f"[RT] Pushed {device['entity_id']} to cloud ({elapsed:.0f}ms)")
                else:
                    logger.warning(f"[RT] Push failed for {device['entity_id']}: {resp.status} ({elapsed:.0f}ms)")
        except Exception as e:
            elapsed = (time.time() - start_time) * 1000 if start_time else 0
            logger.warning(f"[RT] Push error for {device['entity_id']}: {e} ({elapsed:.0f}ms)")

    # ==================== Sensor Polling (Beta Mode) ====================

    async def sensor_poll_loop(self):
        """Poll sensor states and push to cloud at configured interval.

        Only runs when sensor_realtime is False (beta mode).
        Commands and binary sensors still get realtime updates.
        """
        logger.info(f"Starting sensor poll loop (interval: {self.sensor_poll_interval}s)")

        while self.running:
            try:
                await self.push_sensor_batch()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error in sensor poll loop: {e}")

            await asyncio.sleep(self.sensor_poll_interval)

    async def push_sensor_batch(self):
        """Push all sensor states to cloud as a batch."""
        if not self.firebase_credentials:
            return

        # Collect only sensor entities from cache
        sensors = [
            device for entity_id, device in self.devices.items()
            if device.get("domain") == "sensor"
        ]

        if not sensors:
            return

        api_url = f"https://us-central1-{self.firebase_project}.cloudfunctions.net/batchUpdateDeviceStates"

        try:
            async with self.session.post(
                api_url,
                json={
                    "hubId": self.hub_id,
                    "devices": sensors,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                headers={"Authorization": f"Bearer {self.firebase_credentials.get('token', '')}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    logger.info(f"[POLL] Pushed {len(sensors)} sensors to cloud")
                else:
                    logger.warning(f"[POLL] Batch push failed: {resp.status}")
        except Exception as e:
            logger.warning(f"[POLL] Batch push error: {e}")

    # ==================== LAPA-75: Health Heartbeat ====================

    async def heartbeat_loop(self):
        """Send periodic heartbeat to cloud with health metrics."""
        logger.info(f"Starting heartbeat loop (interval: {self.heartbeat_interval}s)")

        while self.running:
            try:
                await self.send_heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error sending heartbeat: {e}")

            await asyncio.sleep(self.heartbeat_interval)

    async def send_heartbeat(self):
        """Send heartbeat with health metrics to cloud."""
        if not self.firebase_credentials:
            return

        # Collect system metrics
        try:
            memory = psutil.virtual_memory()
            cpu_percent = psutil.cpu_percent(interval=0.1)
        except Exception:
            memory = None
            cpu_percent = 0

        uptime = (datetime.now(timezone.utc) - self.start_time).total_seconds()

        heartbeat_data = {
            "hubId": self.hub_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "online",
            "firmwareVersion": self.version_string,  # addon:HA format for remote tracking
            "metrics": {
                "uptime_seconds": int(uptime),
                "addon_version": self.addon_version,
                "ha_version": self.ha_version,
                "device_count": len(self.devices),
                "scene_count": len(self.scenes),
                "automation_count": len(self.automations),
                "cpu_percent": cpu_percent,
                "memory_percent": memory.percent if memory else 0,
                "memory_used_mb": memory.used // (1024 * 1024) if memory else 0,
                "last_device_sync": self.last_device_sync.isoformat() if self.last_device_sync else None,
                "last_energy_report": self.last_energy_report.isoformat() if self.last_energy_report else None,
                "sync_error_count": self.sync_error_count,
                "energy_error_count": self.energy_error_count,
                "websocket_connected": self.ha_websocket is not None and not self.ha_websocket.closed,
            },
        }

        api_url = f"https://us-central1-{self.firebase_project}.cloudfunctions.net/hubHeartbeat"

        try:
            async with self.session.post(
                api_url,
                json=heartbeat_data,
                headers={"Authorization": f"Bearer {self.firebase_credentials.get('token', '')}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    logger.debug("Heartbeat sent successfully")
                else:
                    logger.debug(f"Heartbeat failed: {resp.status}")
        except Exception as e:
            logger.debug(f"Could not send heartbeat: {e}")

    # ==================== LAPA-78: Scenes and Automations ====================

    async def scene_sync_loop(self):
        """Periodically sync scenes and automations from HA to cloud."""
        logger.info("Starting scene/automation sync loop")

        while self.running:
            try:
                await self.sync_scenes_and_automations()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log_activity(f"Error syncing scenes: {e}", "error")

            # Sync every 5 minutes (less frequent than devices)
            await asyncio.sleep(300)

    async def sync_scenes_and_automations(self):
        """Sync all scenes and automations from Home Assistant."""
        states = await self.get_ha_states()

        if not states:
            return

        scenes = []
        automations = []

        for state in states:
            entity_id = state.get("entity_id", "")
            domain = entity_id.split(".")[0] if "." in entity_id else ""
            attributes = state.get("attributes", {})

            if domain == "scene":
                scene = {
                    "entity_id": entity_id,
                    "name": attributes.get("friendly_name", entity_id),
                    "icon": attributes.get("icon"),
                }
                scenes.append(scene)
                self.scenes[entity_id] = scene

            elif domain == "automation":
                automation = {
                    "entity_id": entity_id,
                    "name": attributes.get("friendly_name", entity_id),
                    "state": state.get("state"),  # on/off
                    "last_triggered": attributes.get("last_triggered"),
                    "mode": attributes.get("mode"),
                }
                automations.append(automation)
                self.automations[entity_id] = automation

        if scenes or automations:
            self.log_activity(f"Synced {len(scenes)} scenes, {len(automations)} automations")
            await self.push_scenes_to_cloud(scenes, automations)

    async def push_scenes_to_cloud(self, scenes: list, automations: list):
        """Push scenes and automations to LapaHub cloud."""
        if not self.firebase_credentials:
            return

        api_url = f"https://us-central1-{self.firebase_project}.cloudfunctions.net/syncScenes"

        try:
            async with self.session.post(
                api_url,
                json={
                    "hubId": self.hub_id,
                    "scenes": scenes,
                    "automations": automations,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                headers={"Authorization": f"Bearer {self.firebase_credentials.get('token', '')}"},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status == 200:
                    logger.debug(f"Pushed {len(scenes)} scenes and {len(automations)} automations to cloud")
                else:
                    logger.warning(f"Scene sync failed: {resp.status}")
        except Exception as e:
            logger.warning(f"Could not push scenes to cloud: {e}")

    async def trigger_scene(self, entity_id: str) -> bool:
        """Trigger a scene in Home Assistant."""
        return await self.call_ha_service("scene", "turn_on", {"entity_id": entity_id})

    async def toggle_automation(self, entity_id: str, enable: bool) -> bool:
        """Enable or disable an automation."""
        service = "turn_on" if enable else "turn_off"
        return await self.call_ha_service("automation", service, {"entity_id": entity_id})

    async def get_ha_states(self) -> list:
        """Get all entity states from Home Assistant."""
        headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}

        try:
            async with self.session.get(
                f"{HA_BASE_URL}/api/states",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    logger.error(f"Failed to get HA states: {resp.status}")
                    return []
        except Exception as e:
            logger.error(f"Error getting HA states: {e}")
            return []

    async def call_ha_service(self, domain: str, service: str, data: dict) -> bool:
        """Call a Home Assistant service."""
        headers = {
            "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
            "Content-Type": "application/json",
        }

        try:
            async with self.session.post(
                f"{HA_BASE_URL}/api/services/{domain}/{service}",
                headers=headers,
                json=data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status in (200, 201):
                    logger.info(f"Service called: {domain}.{service}")
                    return True
                else:
                    logger.error(f"Service call failed: {resp.status}")
                    return False
        except Exception as e:
            logger.error(f"Error calling service: {e}")
            return False

    async def fetch_energy_prefs(self):
        """Fetch Energy Dashboard preferences from Home Assistant via REST API."""
        try:
            async with self.session.get(
                f"{HA_BASE_URL}/api/energy/prefs",
                headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    self.energy_prefs = await resp.json()
                    self.energy_prefs_last_fetch = datetime.now(timezone.utc)
                    sources = self.energy_prefs.get("energy_sources", [])
                    logger.info(f"Loaded Energy Dashboard config with {len(sources)} sources")
                    if sources:
                        self.log_activity(f"Energy Dashboard: {len(sources)} sources configured")
                        # Log the source types for debugging
                        for source in sources[:5]:  # Log first 5
                            source_type = source.get("type", "unknown")
                            stat_id = source.get("stat_energy_from") or source.get("stat_energy_to") or "N/A"
                            logger.debug(f"  - {source_type}: {stat_id}")
                    return True
                else:
                    logger.debug(f"Energy prefs API returned {resp.status}")
                    return False
        except Exception as e:
            logger.debug(f"Could not fetch energy preferences: {e}")
            return False

    async def device_sync_loop(self):
        """Periodically sync devices from HA to cloud."""
        logger.info(f"Starting device sync loop (interval: {self.sync_interval}s)")
        consecutive_errors = 0

        while self.running:
            try:
                await self.sync_devices()
                self.last_device_sync = datetime.now(timezone.utc)
                consecutive_errors = 0  # Reset on success
            except asyncio.CancelledError:
                raise
            except Exception as e:
                consecutive_errors += 1
                self.sync_error_count += 1
                self.log_activity(f"Device sync error ({consecutive_errors}): {e}", "error")

                # If too many consecutive errors, try to re-authenticate
                if consecutive_errors >= 3:
                    self.log_activity("Too many sync errors, attempting re-authentication", "warning")
                    await self.authenticate_with_retry()
                    consecutive_errors = 0

            await asyncio.sleep(self.sync_interval)

    async def sync_devices(self):
        """Sync all devices from Home Assistant to LapaHub cloud."""
        states = await self.get_ha_states()

        if not states:
            return

        # Filter to relevant domains
        relevant_domains = [
            "switch", "light", "climate", "cover", "fan",
            "sensor", "binary_sensor", "lock", "media_player",
        ]

        devices = []
        for state in states:
            entity_id = state.get("entity_id", "")
            domain = entity_id.split(".")[0] if "." in entity_id else ""

            if domain not in relevant_domains:
                continue

            attributes = state.get("attributes", {})

            device = {
                "entity_id": entity_id,
                "domain": domain,
                "friendly_name": attributes.get("friendly_name", entity_id),
                "device_class": attributes.get("device_class"),
                "state": state.get("state"),
                "attributes": attributes,
                "last_updated": state.get("last_updated"),
            }
            devices.append(device)

        self.devices = {d["entity_id"]: d for d in devices}
        logger.info(f"Synced {len(devices)} devices from Home Assistant")

        # Send to cloud
        await self.push_devices_to_cloud(devices)

    async def push_devices_to_cloud(self, devices: list):
        """Push device list to LapaHub cloud."""
        if not self.firebase_credentials:
            logger.debug("Skipping cloud push (no credentials)")
            return

        api_url = f"https://us-central1-{self.firebase_project}.cloudfunctions.net/syncDevices"

        try:
            async with self.session.post(
                api_url,
                json={
                    "hubId": self.hub_id,
                    "devices": devices,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                headers={"Authorization": f"Bearer {self.firebase_credentials.get('token', '')}"},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status == 200:
                    logger.debug(f"Pushed {len(devices)} devices to cloud")
                else:
                    logger.warning(f"Cloud push failed: {resp.status}")
        except Exception as e:
            logger.warning(f"Could not push to cloud: {e}")

    async def command_listener_loop(self):
        """Listen for commands from LapaHub cloud."""
        logger.info("Starting command listener loop")
        consecutive_errors = 0

        while self.running:
            try:
                await self.poll_commands()
                consecutive_errors = 0  # Reset on success
            except asyncio.CancelledError:
                raise
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors <= 3:  # Only log first few errors
                    logger.error(f"Error polling commands: {e}")

                # If too many consecutive errors, try to re-authenticate
                if consecutive_errors >= 5:
                    self.log_activity("Too many command poll errors, attempting re-authentication", "warning")
                    await self.authenticate_with_retry()
                    consecutive_errors = 0

            await asyncio.sleep(5)  # Poll every 5 seconds

    async def poll_commands(self):
        """Poll for pending commands from cloud."""
        if not self.firebase_credentials:
            return

        api_url = f"https://us-central1-{self.firebase_project}.cloudfunctions.net/getPendingCommands"

        try:
            async with self.session.get(
                api_url,
                params={"hubId": self.hub_id},
                headers={"Authorization": f"Bearer {self.firebase_credentials.get('token', '')}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    commands = data.get("commands", [])

                    for cmd in commands:
                        await self.execute_command(cmd)
        except Exception as e:
            logger.debug(f"Command poll error: {e}")

    async def execute_command(self, command: dict):
        """Execute a command from the cloud."""
        cmd_id = command.get("id")
        entity_id = command.get("entity_id")
        action = command.get("action")
        params = command.get("params", {})

        logger.info(f"Executing command {cmd_id}: {action} on {entity_id}")

        # Parse entity domain
        domain = entity_id.split(".")[0] if "." in entity_id else "homeassistant"

        # Map common actions to HA services
        service_map = {
            "turn_on": "turn_on",
            "turn_off": "turn_off",
            "toggle": "toggle",
            "set_temperature": "set_temperature",
            "set_hvac_mode": "set_hvac_mode",
            "lock": "lock",
            "unlock": "unlock",
            "open": "open_cover",
            "close": "close_cover",
        }

        service = service_map.get(action, action)
        service_data = {"entity_id": entity_id, **params}

        success = await self.call_ha_service(domain, service, service_data)

        # Report command result back to cloud
        await self.report_command_result(cmd_id, success)

    async def report_command_result(self, cmd_id: str, success: bool):
        """Report command execution result to cloud."""
        if not self.firebase_credentials:
            return

        api_url = f"https://us-central1-{self.firebase_project}.cloudfunctions.net/reportCommandResult"

        try:
            await self.session.post(
                api_url,
                json={
                    "commandId": cmd_id,
                    "hubId": self.hub_id,
                    "success": success,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                headers={"Authorization": f"Bearer {self.firebase_credentials.get('token', '')}"},
                timeout=aiohttp.ClientTimeout(total=30),
            )
        except Exception as e:
            logger.warning(f"Could not report command result: {e}")

    async def energy_report_loop(self):
        """Periodically report energy data to cloud."""
        logger.info(f"Starting energy report loop (interval: {self.energy_interval}s)")
        consecutive_errors = 0

        while self.running:
            try:
                await self.report_energy()
                self.last_energy_report = datetime.now(timezone.utc)
                consecutive_errors = 0  # Reset on success
            except asyncio.CancelledError:
                raise
            except Exception as e:
                consecutive_errors += 1
                self.energy_error_count += 1
                self.log_activity(f"Energy report error ({consecutive_errors}): {e}", "error")

                # If too many consecutive errors, try to re-authenticate
                if consecutive_errors >= 3:
                    self.log_activity("Too many energy errors, attempting re-authentication", "warning")
                    await self.authenticate_with_retry()
                    consecutive_errors = 0

            await asyncio.sleep(self.energy_interval)

    async def report_energy(self):
        """Collect and report energy data using HA Energy Dashboard config."""
        # Refresh energy prefs periodically (every 5 minutes or if not loaded)
        should_refresh = (
            not self.energy_prefs or
            not self.energy_prefs_last_fetch or
            (datetime.now(timezone.utc) - self.energy_prefs_last_fetch) > timedelta(minutes=5)
        )
        if should_refresh:
            await self.fetch_energy_prefs()

        states = await self.get_ha_states()

        if not states:
            return

        # Build state lookup for fast access - handle both entity_id and stat_id formats
        state_lookup = {}
        for s in states:
            entity_id = s.get("entity_id", "")
            state_lookup[entity_id] = s
            # Also index without "sensor." prefix for stat_id matching
            if entity_id.startswith("sensor."):
                state_lookup[entity_id[7:]] = s

        energy_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sensors": {},
            "dashboard_sources": {},  # Mapped from HA Energy Dashboard
        }

        # Debug: log energy_prefs status
        logger.info(f"energy_prefs status: exists={self.energy_prefs is not None}, "
                    f"last_fetch={self.energy_prefs_last_fetch}")
        if self.energy_prefs:
            logger.info(f"energy_prefs keys: {list(self.energy_prefs.keys())}")

        # If we have Energy Dashboard preferences, use those sensors explicitly
        if self.energy_prefs:
            sources = self.energy_prefs.get("energy_sources", [])
            logger.info(f"Processing {len(sources)} energy sources from dashboard config")

            # Debug: log first few state_lookup keys
            sample_keys = list(state_lookup.keys())[:10]
            logger.info(f"Sample state_lookup keys: {sample_keys}")

            for source in sources:
                source_type = source.get("type")
                # Try multiple stat_id fields
                stat_id = (
                    source.get("stat_energy_from") or
                    source.get("stat_energy_to") or
                    source.get("entity_id") or
                    source.get("stat_compensation")
                )

                logger.info(f"Energy source: type={source_type}, stat_id={stat_id}, source_keys={list(source.keys())}")

                if not stat_id:
                    continue

                # Try to find the state - handle various stat_id formats
                state = None
                # Direct lookup
                if stat_id in state_lookup:
                    state = state_lookup[stat_id]
                    logger.info(f"Found {stat_id} via direct lookup")
                # Try with sensor. prefix
                elif f"sensor.{stat_id}" in state_lookup:
                    state = state_lookup[f"sensor.{stat_id}"]
                    logger.info(f"Found {stat_id} via sensor. prefix")
                # Try stripping recorder: prefix
                elif stat_id.startswith("recorder:"):
                    clean_id = stat_id.replace("recorder:", "")
                    if clean_id in state_lookup:
                        state = state_lookup[clean_id]
                        logger.info(f"Found {stat_id} via recorder: strip")
                else:
                    logger.warning(f"Could not find {stat_id} in state_lookup")

                if state:
                    try:
                        state_value = state.get("state", "")
                        if state_value not in ("unknown", "unavailable", ""):
                            value = float(state_value)
                            energy_data["dashboard_sources"][stat_id] = {
                                "type": source_type,  # solar, grid, battery, gas
                                "value": value,
                                "unit": state.get("attributes", {}).get("unit_of_measurement"),
                                "friendly_name": state.get("attributes", {}).get("friendly_name"),
                            }
                    except (ValueError, TypeError) as e:
                        logger.debug(f"Could not parse {stat_id}: {e}")
                else:
                    logger.debug(f"Energy source {stat_id} not found in states")

            # Also check for device consumption sensors
            device_consumption = self.energy_prefs.get("device_consumption", [])
            for device in device_consumption:
                stat_id = device.get("stat_consumption")
                if stat_id and stat_id in state_lookup:
                    state = state_lookup[stat_id]
                    try:
                        state_value = state.get("state", "")
                        if state_value not in ("unknown", "unavailable", ""):
                            value = float(state_value)
                            energy_data["dashboard_sources"][stat_id] = {
                                "type": "device",
                                "value": value,
                                "unit": state.get("attributes", {}).get("unit_of_measurement"),
                                "friendly_name": state.get("attributes", {}).get("friendly_name"),
                            }
                    except (ValueError, TypeError):
                        pass

            if energy_data["dashboard_sources"]:
                logger.info(f"Using {len(energy_data['dashboard_sources'])} Energy Dashboard sources")
            elif sources:
                logger.warning(f"Energy Dashboard has {len(sources)} sources but none matched current states")

        # Also collect all energy-related sensors as backup (for pattern matching fallback)
        energy_classes = ["energy", "power", "voltage", "current", "battery"]

        for state in states:
            entity_id = state.get("entity_id", "")
            if not entity_id.startswith("sensor."):
                continue

            attributes = state.get("attributes", {})
            device_class = attributes.get("device_class")

            if device_class in energy_classes:
                try:
                    state_value = state.get("state", "")
                    if state_value not in ("unknown", "unavailable", ""):
                        value = float(state_value)
                        energy_data["sensors"][entity_id] = {
                            "value": value,
                            "unit": attributes.get("unit_of_measurement"),
                            "device_class": device_class,
                            "friendly_name": attributes.get("friendly_name"),
                        }
                except (ValueError, TypeError):
                    pass

        source_count = len(energy_data.get("dashboard_sources", {}))
        sensor_count = len(energy_data.get("sensors", {}))
        logger.info(f"Collected {source_count} dashboard sources, {sensor_count} sensors")
        await self.push_energy_to_cloud(energy_data)

    async def push_energy_to_cloud(self, energy_data: dict):
        """Push energy data to LapaHub cloud."""
        if not self.firebase_credentials:
            logger.debug("Skipping energy push (no credentials)")
            return

        api_url = f"https://us-central1-{self.firebase_project}.cloudfunctions.net/reportEnergy"

        try:
            async with self.session.post(
                api_url,
                json={"hubId": self.hub_id, **energy_data},
                headers={"Authorization": f"Bearer {self.firebase_credentials.get('token', '')}"},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status == 200:
                    logger.debug("Energy data pushed to cloud")
                else:
                    logger.warning(f"Energy push failed: {resp.status}")
        except Exception as e:
            logger.warning(f"Could not push energy data: {e}")

    async def run_web_server(self):
        """Run the ingress web interface."""
        app = web.Application()
        app.router.add_get("/", self.handle_index)
        app.router.add_get("/api/status", self.handle_status)
        app.router.add_get("/api/devices", self.handle_devices)
        app.router.add_get("/api/scenes", self.handle_scenes)
        app.router.add_get("/api/activity", self.handle_activity)

        self.web_runner = web.AppRunner(app)
        await self.web_runner.setup()

        site = web.TCPSite(self.web_runner, "0.0.0.0", 8099)
        await site.start()

        logger.info("Web interface started on port 8099")

        # Keep running
        while self.running:
            await asyncio.sleep(60)

    async def handle_index(self, request):
        """Serve the main web interface."""
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>LapaHub Bridge</title>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                * { box-sizing: border-box; }
                body { font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 20px; background: #f0f2f5; }
                .container { max-width: 1000px; margin: 0 auto; }
                .header { background: linear-gradient(135deg, #1e3a5f 0%, #2d5a87 100%); color: white; padding: 25px; border-radius: 12px; margin-bottom: 20px; }
                .header h1 { margin: 0 0 5px 0; font-size: 24px; }
                .header p { margin: 0; opacity: 0.8; }
                .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 20px; }
                .card { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
                .card h3 { margin: 0 0 15px 0; font-size: 14px; color: #666; text-transform: uppercase; letter-spacing: 0.5px; }
                .stat-value { font-size: 32px; font-weight: bold; color: #1e3a5f; }
                .stat-label { font-size: 12px; color: #888; margin-top: 5px; }
                .status-indicator { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 8px; }
                .status-online { background: #22c55e; box-shadow: 0 0 8px rgba(34, 197, 94, 0.5); }
                .status-offline { background: #ef4444; }
                .status-warning { background: #f59e0b; }
                .activity-list { max-height: 300px; overflow-y: auto; }
                .activity-item { padding: 10px 0; border-bottom: 1px solid #eee; font-size: 13px; }
                .activity-item:last-child { border-bottom: none; }
                .activity-time { color: #888; font-size: 11px; }
                .activity-error { color: #ef4444; }
                .activity-warning { color: #f59e0b; }
                .metrics-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
                .metric { padding: 10px; background: #f8fafc; border-radius: 6px; }
                .metric-label { font-size: 11px; color: #666; }
                .metric-value { font-size: 16px; font-weight: 600; color: #1e3a5f; }
                .tabs { display: flex; gap: 10px; margin-bottom: 15px; }
                .tab { padding: 8px 16px; background: #e2e8f0; border: none; border-radius: 6px; cursor: pointer; font-size: 13px; }
                .tab.active { background: #1e3a5f; color: white; }
                .tab-content { display: none; }
                .tab-content.active { display: block; }
                .device-list { max-height: 400px; overflow-y: auto; }
                .device-item { display: flex; justify-content: space-between; align-items: center; padding: 12px; border-bottom: 1px solid #eee; }
                .device-name { font-weight: 500; }
                .device-state { padding: 4px 8px; border-radius: 4px; font-size: 12px; }
                .device-state.on { background: #dcfce7; color: #166534; }
                .device-state.off { background: #f3f4f6; color: #6b7280; }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>LapaHub Bridge</h1>
                    <p>Connecting Home Assistant to LapaHub Cloud</p>
                </div>

                <div class="grid">
                    <div class="card">
                        <h3>Connection Status</h3>
                        <div>
                            <span class="status-indicator" id="ha-status"></span>
                            <span id="ha-status-text">Checking...</span>
                        </div>
                        <div style="margin-top: 10px;">
                            <span class="status-indicator" id="cloud-status"></span>
                            <span id="cloud-status-text">Checking...</span>
                        </div>
                        <div style="margin-top: 10px;">
                            <span class="status-indicator" id="ws-status"></span>
                            <span id="ws-status-text">Checking...</span>
                        </div>
                    </div>
                    <div class="card">
                        <h3>Devices</h3>
                        <div class="stat-value" id="device-count">-</div>
                        <div class="stat-label">Synced to Cloud</div>
                    </div>
                    <div class="card">
                        <h3>Scenes</h3>
                        <div class="stat-value" id="scene-count">-</div>
                        <div class="stat-label">Available</div>
                    </div>
                    <div class="card">
                        <h3>Automations</h3>
                        <div class="stat-value" id="automation-count">-</div>
                        <div class="stat-label">Active</div>
                    </div>
                </div>

                <div class="card">
                    <h3>System Metrics</h3>
                    <div class="metrics-grid">
                        <div class="metric">
                            <div class="metric-label">Uptime</div>
                            <div class="metric-value" id="uptime">-</div>
                        </div>
                        <div class="metric">
                            <div class="metric-label">CPU Usage</div>
                            <div class="metric-value" id="cpu">-</div>
                        </div>
                        <div class="metric">
                            <div class="metric-label">Memory Usage</div>
                            <div class="metric-value" id="memory">-</div>
                        </div>
                        <div class="metric">
                            <div class="metric-label">Version (Addon:HA)</div>
                            <div class="metric-value" id="version" style="font-size: 12px;">-</div>
                        </div>
                        <div class="metric">
                            <div class="metric-label">Hub ID</div>
                            <div class="metric-value" id="hub-id" style="font-size: 12px;">-</div>
                        </div>
                        <div class="metric">
                            <div class="metric-label">Last Device Sync</div>
                            <div class="metric-value" id="last-sync" style="font-size: 12px;">-</div>
                        </div>
                        <div class="metric">
                            <div class="metric-label">Last Energy Report</div>
                            <div class="metric-value" id="last-energy" style="font-size: 12px;">-</div>
                        </div>
                    </div>
                </div>

                <div class="card">
                    <div class="tabs">
                        <button class="tab active" onclick="showTab('activity')">Activity Log</button>
                        <button class="tab" onclick="showTab('devices')">Devices</button>
                    </div>
                    <div id="activity-tab" class="tab-content active">
                        <div class="activity-list" id="activity-list">Loading...</div>
                    </div>
                    <div id="devices-tab" class="tab-content">
                        <div class="device-list" id="device-list">Loading...</div>
                    </div>
                </div>
            </div>

            <script>
                function showTab(tab) {
                    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
                    event.target.classList.add('active');
                    document.getElementById(tab + '-tab').classList.add('active');
                }

                function formatUptime(seconds) {
                    const days = Math.floor(seconds / 86400);
                    const hours = Math.floor((seconds % 86400) / 3600);
                    const mins = Math.floor((seconds % 3600) / 60);
                    if (days > 0) return days + 'd ' + hours + 'h';
                    if (hours > 0) return hours + 'h ' + mins + 'm';
                    return mins + 'm';
                }

                function formatTime(isoString) {
                    if (!isoString) return 'Never';
                    const date = new Date(isoString);
                    return date.toLocaleTimeString();
                }

                async function updateStatus() {
                    try {
                        const resp = await fetch('api/status');
                        const data = await resp.json();

                        document.getElementById('device-count').textContent = data.device_count;
                        document.getElementById('scene-count').textContent = data.scene_count || 0;
                        document.getElementById('automation-count').textContent = data.automation_count || 0;
                        document.getElementById('version').textContent = data.version || '-';
                        document.getElementById('hub-id').textContent = data.hub_id || 'Not configured';
                        document.getElementById('uptime').textContent = formatUptime(data.uptime_seconds || 0);
                        document.getElementById('cpu').textContent = (data.cpu_percent || 0).toFixed(1) + '%';
                        document.getElementById('memory').textContent = (data.memory_percent || 0).toFixed(1) + '%';
                        document.getElementById('last-sync').textContent = formatTime(data.last_device_sync);
                        document.getElementById('last-energy').textContent = formatTime(data.last_energy_report);

                        // HA Status
                        const haStatus = document.getElementById('ha-status');
                        const haText = document.getElementById('ha-status-text');
                        if (data.connected) {
                            haStatus.className = 'status-indicator status-online';
                            haText.textContent = 'Home Assistant Connected';
                        } else {
                            haStatus.className = 'status-indicator status-offline';
                            haText.textContent = 'Home Assistant Disconnected';
                        }

                        // Cloud Status
                        const cloudStatus = document.getElementById('cloud-status');
                        const cloudText = document.getElementById('cloud-status-text');
                        if (data.cloud_authenticated) {
                            cloudStatus.className = 'status-indicator status-online';
                            cloudText.textContent = 'Cloud Authenticated';
                        } else {
                            cloudStatus.className = 'status-indicator status-offline';
                            cloudText.textContent = 'Cloud Not Connected';
                        }

                        // WebSocket Status
                        const wsStatus = document.getElementById('ws-status');
                        const wsText = document.getElementById('ws-status-text');
                        if (data.websocket_connected) {
                            wsStatus.className = 'status-indicator status-online';
                            wsText.textContent = 'Real-time Updates Active';
                        } else {
                            wsStatus.className = 'status-indicator status-warning';
                            wsText.textContent = 'Real-time Updates Inactive';
                        }
                    } catch(e) {
                        console.error('Status error:', e);
                    }
                }

                async function updateActivity() {
                    try {
                        const resp = await fetch('api/activity');
                        const data = await resp.json();
                        const list = document.getElementById('activity-list');

                        if (data.activities && data.activities.length > 0) {
                            list.innerHTML = data.activities.map(a => {
                                const levelClass = a.level === 'error' ? 'activity-error' : (a.level === 'warning' ? 'activity-warning' : '');
                                return '<div class="activity-item ' + levelClass + '">' +
                                    '<span class="activity-time">' + formatTime(a.timestamp) + '</span> ' +
                                    a.message + '</div>';
                            }).join('');
                        } else {
                            list.innerHTML = '<div class="activity-item">No recent activity</div>';
                        }
                    } catch(e) {
                        console.error('Activity error:', e);
                    }
                }

                async function updateDevices() {
                    try {
                        const resp = await fetch('api/devices');
                        const data = await resp.json();
                        const list = document.getElementById('device-list');

                        if (data.devices && data.devices.length > 0) {
                            list.innerHTML = data.devices.slice(0, 50).map(d => {
                                const stateClass = d.state === 'on' ? 'on' : 'off';
                                return '<div class="device-item">' +
                                    '<div><span class="device-name">' + d.friendly_name + '</span><br>' +
                                    '<span style="color:#888;font-size:11px">' + d.entity_id + '</span></div>' +
                                    '<span class="device-state ' + stateClass + '">' + d.state + '</span></div>';
                            }).join('');
                        } else {
                            list.innerHTML = '<div class="device-item">No devices synced</div>';
                        }
                    } catch(e) {
                        console.error('Devices error:', e);
                    }
                }

                updateStatus();
                updateActivity();
                updateDevices();
                setInterval(updateStatus, 5000);
                setInterval(updateActivity, 10000);
                setInterval(updateDevices, 30000);
            </script>
        </body>
        </html>
        """
        return web.Response(text=html, content_type="text/html")

    async def handle_status(self, request):
        """Return current status with all metrics."""
        token_expires = None
        if self.token_expires_at:
            token_expires = self.token_expires_at.isoformat()

        # Collect system metrics
        try:
            memory = psutil.virtual_memory()
            cpu_percent = psutil.cpu_percent(interval=0.1)
        except Exception:
            memory = None
            cpu_percent = 0

        uptime = (datetime.now(timezone.utc) - self.start_time).total_seconds()

        return web.json_response({
            "hub_id": self.hub_id,
            "version": self.version_string,
            "addon_version": self.addon_version,
            "ha_version": self.ha_version,
            "device_count": len(self.devices),
            "scene_count": len(self.scenes),
            "automation_count": len(self.automations),
            "connected": bool(SUPERVISOR_TOKEN),
            "cloud_authenticated": bool(self.firebase_credentials),
            "websocket_connected": self.ha_websocket is not None and not self.ha_websocket.closed,
            "token_expires_at": token_expires,
            "retry_count": self.current_retry_count,
            "uptime_seconds": int(uptime),
            "cpu_percent": cpu_percent,
            "memory_percent": memory.percent if memory else 0,
            "last_device_sync": self.last_device_sync.isoformat() if self.last_device_sync else None,
            "last_energy_report": self.last_energy_report.isoformat() if self.last_energy_report else None,
            "sync_error_count": self.sync_error_count,
            "energy_error_count": self.energy_error_count,
            "last_sync": datetime.now(timezone.utc).isoformat(),
        })

    async def handle_devices(self, request):
        """Return synced devices."""
        return web.json_response({"devices": list(self.devices.values())})

    async def handle_scenes(self, request):
        """Return synced scenes and automations."""
        return web.json_response({
            "scenes": list(self.scenes.values()),
            "automations": list(self.automations.values()),
        })

    async def handle_activity(self, request):
        """Return recent activity log."""
        return web.json_response({
            "activities": list(reversed(list(self.activity_log))),
        })


async def main():
    """Main entry point."""
    addon = LapaHubAddon()
    await addon.start()


if __name__ == "__main__":
    asyncio.run(main())
