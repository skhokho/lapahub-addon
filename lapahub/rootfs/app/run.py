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
from reliability import CommandExpired, CommandJournal, StatePublisher, check_command_deadline
from managed_scenes import ManagedScenes
from energy_payload import EnergyDeliveryError, collect_energy_payload

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
def _read_addon_version() -> str:
    """Read addon version from HA Supervisor API, falling back to config.yaml."""
    import urllib.request

    # Method 1: Supervisor API (most accurate — reflects installed version)
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        try:
            token = Path("/run/supervisor/token").read_text().strip()
        except Exception:
            pass
    if not token:
        try:
            token = Path("/run/os/supervisor/token").read_text().strip()
        except Exception:
            pass
    if token:
        try:
            req = urllib.request.Request(
                "http://supervisor/addons/self/info",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                import json
                data = json.loads(resp.read())
                version = data.get("data", {}).get("version")
                if version:
                    return version
        except Exception as e:
            logger.warning(f"Could not read version from Supervisor API: {e}")

    # Method 2: Read from config.yaml (always in sync with deployed version)
    try:
        import yaml
        config_paths = [
            Path("/config.yaml"),           # Inside addon container
            Path("/data/config.yaml"),
            Path(__file__).parent.parent.parent / "config.yaml",  # Relative to rootfs/app/
        ]
        for config_path in config_paths:
            if config_path.exists():
                with open(config_path) as f:
                    config = yaml.safe_load(f)
                    version = config.get("version")
                    if version:
                        return str(version)
    except Exception as e:
        logger.warning(f"Could not read version from config.yaml: {e}")

    return "1.0.53"  # Last resort fallback — keep in sync with config.yaml

ADDON_VERSION = _read_addon_version()


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
    logger.info("Supervisor credentials available")
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
        self.command_journal = CommandJournal("/data/command_journal.sqlite3")
        self.managed_scenes = ManagedScenes("/data/managed_scenes.sqlite3")
        self.scene_lock = asyncio.Lock()
        self.automation_refresh_wake = asyncio.Event()
        self.scene_restore_requested = asyncio.Event()
        self.state_publisher = StatePublisher(self.push_state_change_to_cloud)
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

        # Physical devices from HA device registry (Parent Device Architecture)
        self.physical_devices: dict = {}
        # Entity ID → Device ID mapping from entity registry
        self.entity_device_map: dict = {}

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
        """Get combined version string (addon:HA) for troubleshooting."""
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
                self.command_ack_loop(),
                self.state_publisher.worker(),
                self.state_publisher.worker(),
                self.state_publisher.worker(),
                self.energy_report_loop(),
                self.token_refresh_loop(),
                self.ha_websocket_loop(),      # LAPA-74: Real-time state updates
                self.heartbeat_loop(),          # LAPA-75: Health heartbeat
                self.automation_refresh_loop(),
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

        self.managed_scenes.close()
        self.command_journal.close()
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
                    logger.error("Authentication failed (HTTP %s)", resp.status)
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
        """Pace every disconnect, including caught handshake/auth failures."""
        logger.info("Starting Home Assistant WebSocket loop")
        delay = 2
        while self.running:
            healthy = False
            try:
                healthy = await self.connect_ha_websocket()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log_activity("WebSocket connection unavailable", "warning")
            if not self.running:
                return
            if healthy:
                delay = 2
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def connect_ha_websocket(self):
        """Connect and report whether authentication stayed healthy for 30 seconds."""
        ws_url = f"{HA_BASE_URL.replace('http://', 'ws://').replace('https://', 'wss://')}/api/websocket"

        authenticated_at = None
        try:
            self.ha_websocket = await asyncio.wait_for(self.session.ws_connect(ws_url), timeout=10)
            self.log_activity("Connected to Home Assistant WebSocket")

            # Never subscribe or restore on an unexpected/failed auth greeting.
            auth_msg = await asyncio.wait_for(self.ha_websocket.receive_json(), timeout=10)
            if auth_msg.get("type") != "auth_required":
                raise RuntimeError("Unexpected Home Assistant authentication greeting")
            await self.ha_websocket.send_json({"type": "auth", "access_token": SUPERVISOR_TOKEN})
            auth_result = await asyncio.wait_for(self.ha_websocket.receive_json(), timeout=10)
            if auth_result.get("type") != "auth_ok":
                raise RuntimeError("Home Assistant WebSocket authentication rejected")
            authenticated_at = asyncio.get_running_loop().time()
            self.log_activity("WebSocket authenticated")
            self.scene_restore_requested.set()
            self.automation_refresh_wake.set()

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
            try:
                if self.ha_websocket and not self.ha_websocket.closed:
                    await asyncio.wait_for(self.ha_websocket.close(), timeout=5)
            except Exception:
                self.log_activity("WebSocket close did not complete cleanly", "warning")
            finally:
                self.ha_websocket = None
        return authenticated_at is not None and asyncio.get_running_loop().time() - authenticated_at >= 30

    async def handle_ws_message(self, data: dict):
        """Handle incoming WebSocket message from Home Assistant."""
        msg_type = data.get("type")

        if msg_type == "event":
            event = data.get("event", {})
            event_type = event.get("event_type")

            if event_type == "state_changed":
                await self.handle_state_change(event.get("data", {}))
            elif event_type == "homeassistant_started":
                self.scene_restore_requested.set()
                self.automation_refresh_wake.set()
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
            self.state_publisher.offer(device, start_time)
            if self.state_publisher.dropped:
                logger.warning("State publication overflow: dropped=%s queued=%s", self.state_publisher.dropped, len(self.state_publisher.pending))

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
                    return True
                else:
                    logger.warning(f"[RT] Push failed for {device['entity_id']}: {resp.status} ({elapsed:.0f}ms)")
        except Exception as e:
            elapsed = (time.time() - start_time) * 1000 if start_time else 0
            logger.warning(f"[RT] Push error for {device['entity_id']}: {e} ({elapsed:.0f}ms)")

        return False

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

    async def automation_refresh_loop(self):
        """One coalescing worker observes async reloads without delaying receipts.

        Five observation/restoration passes per wake burst; slow/offline HA cannot accumulate
        command tasks. Periodic sync remains the fallback after the bounded window.
        """
        while self.running:
            await self.automation_refresh_wake.wait()
            if not self.running:
                return
            self.automation_refresh_wake.clear()
            for delay in (0, 1, 2, 4, 8):
                if delay:
                    try:
                        await asyncio.wait_for(self.automation_refresh_wake.wait(), timeout=delay)
                        self.automation_refresh_wake.clear()
                    except asyncio.TimeoutError:
                        pass
                if not self.running:
                    return
                try:
                    await asyncio.wait_for(self.refresh_automation_bindings(), timeout=8)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Prompt automation binding refresh unavailable; retry window is bounded", exc_info=True)

    async def refresh_automation_bindings(self):
        """Refresh bindings and requested missing scene definitions; never activate."""
        restore_requested = self.scene_restore_requested.is_set()
        if restore_requested:
            self.scene_restore_requested.clear()
        try:
            states = await self.get_ha_states()
            if restore_requested and (not states or not await self.restore_managed_scenes(states)):
                self.scene_restore_requested.set()
        except (Exception, asyncio.CancelledError):
            if restore_requested:
                self.scene_restore_requested.set()
            raise
        if not states:
            return
        automations = []
        for state in states:
            entity_id = state.get("entity_id", "")
            if not entity_id.startswith("automation."):
                continue
            attributes = state.get("attributes", {})
            automations.append({"entity_id": entity_id,
                "friendly_name": attributes.get("friendly_name", entity_id),
                "state": state.get("state"), "last_triggered": attributes.get("last_triggered"),
                "mode": attributes.get("mode")})
        if automations:
            automations = await self.enrich_automation_configs(automations)
            await self.push_scenes_to_cloud([], automations)

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
        if states:
            await self.restore_managed_scenes(states)

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
                    "friendly_name": attributes.get("friendly_name", entity_id),
                    "state": state.get("state"),  # on/off
                    "last_triggered": attributes.get("last_triggered"),
                    "mode": attributes.get("mode"),
                }
                automations.append(automation)
                self.automations[entity_id] = automation

        # Fetch full automation configs (triggers, conditions, actions)
        if automations:
            automations = await self.enrich_automation_configs(automations)

        if scenes or automations:
            self.log_activity(f"Synced {len(scenes)} scenes, {len(automations)} automations")
            await self.push_scenes_to_cloud(scenes, automations)

    async def enrich_automation_configs(self, automations: list) -> list:
        """Fetch full automation configs from HA via WebSocket API."""
        logger.info(f"Fetching automation configs for {len(automations)} automations")
        try:
            ws_url = f"{HA_BASE_URL.replace('http://', 'ws://').replace('https://', 'wss://')}/api/websocket"

            async with self.session.ws_connect(ws_url) as ws:
                # Auth sequence
                msg = await ws.receive_json()
                if msg.get("type") != "auth_required":
                    return automations

                await ws.send_json({
                    "type": "auth",
                    "access_token": SUPERVISOR_TOKEN,
                })

                auth_result = await ws.receive_json()
                if auth_result.get("type") != "auth_ok":
                    logger.warning("WebSocket auth failed for automation config fetch")
                    return automations

                # Fetch config for each automation
                msg_id = 1
                errors = []
                for automation in automations:
                    entity_id = automation.get("entity_id", "")
                    await ws.send_json({
                        "id": msg_id,
                        "type": "automation/config",
                        "entity_id": entity_id,
                    })

                    try:
                        response = await asyncio.wait_for(ws.receive_json(), timeout=5)
                        if response.get("success") and response.get("result"):
                            # Config is nested under result.config
                            config = response["result"].get("config", response["result"])
                            automation["config_id"] = config.get("id")
                            if isinstance(config.get("id"), str) and config["id"].startswith("lapahub_"):
                                automation["observed_config"] = config
                            # Convert HA config format to LapaHub format
                            # HA uses plural keys: triggers, conditions, actions (or singular for older format)
                            triggers = config.get("triggers") or config.get("trigger", [])
                            conditions = config.get("conditions") or config.get("condition", [])
                            actions = config.get("actions") or config.get("action", [])
                            automation["triggers"] = self.convert_ha_triggers(triggers)
                            automation["conditions"] = self.convert_ha_conditions(conditions)
                            automation["actions"] = self.convert_ha_actions(actions)
                            automation["description"] = config.get("description", "")
                        elif not response.get("success"):
                            error = response.get("error", {})
                            errors.append(f"{entity_id}: {error.get('code', 'unknown')} - {error.get('message', 'no message')}")
                    except asyncio.TimeoutError:
                        errors.append(f"{entity_id}: timeout")
                    except Exception as e:
                        errors.append(f"{entity_id}: {e}")

                    msg_id += 1

                # Log errors summary
                if errors:
                    logger.warning(f"Automation config errors: {len(errors)} failures")
                    # Log first few errors for debugging
                    for err in errors[:3]:
                        logger.warning(f"  - {err}")

                # Count how many automations have triggers
                enriched = sum(1 for a in automations if a.get("triggers"))
                logger.info(f"Enriched {enriched}/{len(automations)} automations with config data")

        except Exception as e:
            logger.warning(f"Could not fetch automation configs: {e}")

        return automations

    def convert_ha_triggers(self, ha_triggers: list) -> list:
        """Convert HA trigger format to LapaHub format."""
        triggers = []
        for i, t in enumerate(ha_triggers if isinstance(ha_triggers, list) else [ha_triggers]):
            if not isinstance(t, dict):
                continue
            # HA 2024+ uses 'trigger' key, older uses 'platform'
            platform = t.get("platform") or t.get("trigger", "")
            trigger_type = t.get("type", "")  # For device triggers, this is the action type

            trigger = {
                "id": f"trigger_{i+1}",
                "type": self.map_trigger_platform(platform),
            }

            if platform == "device":
                # Device triggers: e.g., switch turned on/off, light turned on/off
                trigger["type"] = "deviceState"
                trigger["device_id"] = t.get("device_id")
                trigger["domain"] = t.get("domain")
                trigger["trigger_type"] = trigger_type  # e.g., "turned_on", "turned_off"
                # Build a friendly description
                action_desc = trigger_type.replace("_", " ") if trigger_type else "changed"
                domain = t.get("domain", "device")
                trigger["description"] = f"{domain.title()} {action_desc}"
            elif platform == "state":
                trigger["entity_id"] = t.get("entity_id")
                trigger["from"] = t.get("from")
                trigger["to"] = t.get("to")
                if t.get("for"):
                    trigger["for"] = self.duration_to_seconds(t["for"])
                if t.get("attribute"):
                    trigger["attribute"] = t["attribute"]
                    trigger["type"] = "deviceAttribute"
            elif platform == "time":
                trigger["at"] = t.get("at")
            elif platform == "time_pattern":
                trigger["type"] = "timePattern"
                if t.get("hours") is not None and t.get("minutes") is not None:
                    trigger["at"] = f"{t['hours']}:{t['minutes']}"
            elif platform == "sun":
                trigger["event"] = t.get("event", "sunrise")
                if t.get("offset"):
                    trigger["offset"] = self.duration_to_seconds(t["offset"])
            elif platform == "numeric_state":
                trigger["entity_id"] = t.get("entity_id")
                trigger["above"] = t.get("above")
                trigger["below"] = t.get("below")

            triggers.append(trigger)
        return triggers

    def convert_ha_conditions(self, ha_conditions: list) -> list:
        """Convert HA condition format to LapaHub format."""
        conditions = []
        for i, c in enumerate(ha_conditions if isinstance(ha_conditions, list) else [ha_conditions]):
            if not isinstance(c, dict):
                continue
            cond_type = c.get("condition", "")
            condition = {
                "id": f"condition_{i+1}",
                "type": self.map_condition_type(cond_type),
            }

            if cond_type == "state":
                condition["entity_id"] = c.get("entity_id")
                condition["state"] = c.get("state")
                if c.get("attribute"):
                    condition["attribute"] = c["attribute"]
                    condition["attribute_value"] = c.get("state")
            elif cond_type == "numeric_state":
                condition["entity_id"] = c.get("entity_id")
                if c.get("above") is not None:
                    condition["comparison"] = "above"
                    condition["value"] = c["above"]
                elif c.get("below") is not None:
                    condition["comparison"] = "below"
                    condition["value"] = c["below"]
            elif cond_type == "time":
                condition["after"] = c.get("after")
                condition["before"] = c.get("before")
                if c.get("weekday"):
                    condition["type"] = "dayOfWeek"
                    condition["weekdays"] = self.convert_weekdays(c["weekday"])
            elif cond_type == "sun":
                if c.get("after"):
                    condition["after_sun"] = c["after"]
                if c.get("before"):
                    condition["before_sun"] = c["before"]

            conditions.append(condition)
        return conditions

    def convert_ha_actions(self, ha_actions: list) -> list:
        """Convert HA action format to LapaHub format."""
        actions = []
        for i, a in enumerate(ha_actions if isinstance(ha_actions, list) else [ha_actions]):
            if not isinstance(a, dict):
                continue

            action = {"id": f"action_{i+1}"}

            # HA 2024+ uses 'action' key, older uses 'service'
            service = a.get("service") or a.get("action")
            if service:
                parts = service.split(".", 1)
                action["type"] = "deviceService"
                action["domain"] = parts[0] if len(parts) > 1 else service
                action["service"] = parts[1] if len(parts) > 1 else service

                # Handle target entity - check multiple locations
                target = a.get("target", {})
                entity_id = target.get("entity_id") or a.get("entity_id")
                device_id = target.get("device_id") or a.get("device_id")

                if isinstance(entity_id, list):
                    entity_id = entity_id[0] if entity_id else None
                if isinstance(device_id, list):
                    device_id = device_id[0] if device_id else None

                action["entity_id"] = entity_id
                action["device_id"] = device_id

                # Service data - check both 'data' and 'metadata'
                if a.get("data"):
                    action["service_data"] = a["data"]

            elif "delay" in a:
                action["type"] = "delay"
                action["delay"] = self.duration_to_seconds(a["delay"])
            elif "scene" in a:
                action["type"] = "scene"
                action["scene_id"] = a["scene"]
            elif a.get("type") == "device":
                # Device action format (HA 2024+)
                action["type"] = "deviceAction"
                action["device_id"] = a.get("device_id")
                action["domain"] = a.get("domain")
                action["action_type"] = a.get("type")
                # Build a friendly description
                action_type = a.get("type", "control")
                domain = a.get("domain", "device")
                action["description"] = f"{action_type.replace('_', ' ').title()} {domain}"

            actions.append(action)
        return actions

    def map_trigger_platform(self, platform: str) -> str:
        """Map HA trigger platform to LapaHub trigger type."""
        mapping = {
            "state": "deviceState",
            "device": "deviceState",
            "time": "time",
            "time_pattern": "timePattern",
            "sun": "sun",
            "numeric_state": "numericState",
            "event": "manual",
        }
        return mapping.get(platform, "manual")

    def map_condition_type(self, cond_type: str) -> str:
        """Map HA condition type to LapaHub condition type."""
        mapping = {
            "state": "deviceState",
            "numeric_state": "numericState",
            "time": "time",
            "sun": "sun",
            "zone": "zone",
            "template": "template",
        }
        return mapping.get(cond_type, "deviceState")

    def convert_weekdays(self, weekdays: list) -> list:
        """Convert HA weekday names to numbers (1=Mon, 7=Sun)."""
        day_map = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7}
        return [day_map.get(d.lower(), 1) for d in weekdays if isinstance(d, str)]

    def duration_to_seconds(self, duration) -> int:
        """Convert HA duration format to seconds."""
        if isinstance(duration, (int, float)):
            return int(duration)
        if isinstance(duration, str):
            # Parse HH:MM:SS format
            parts = duration.split(":")
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            elif len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
        if isinstance(duration, dict):
            return (
                duration.get("hours", 0) * 3600 +
                duration.get("minutes", 0) * 60 +
                duration.get("seconds", 0)
            )
        return 0

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

    async def restore_managed_scenes(self, states):
        """Restore missing definitions after HA reload/restart. Never activate them."""
        present = {state.get("entity_id") for state in states}
        restored = True
        async with self.scene_lock:
            for entity_id, definition in self.managed_scenes.active().items():
                if entity_id not in present:
                    accepted = await self.call_ha_service("scene", "create", definition)
                    if not accepted:
                        restored = False
                        logger.warning("Managed scene definition could not be restored: %s", entity_id)
        return restored

    async def trigger_scene(self, entity_id: str, expires_at=None) -> bool:
        """Ensure a managed definition exists, then dispatch one activation."""
        async with self.scene_lock:
            check_command_deadline(expires_at)
            definition = self.managed_scenes.active().get(entity_id)
            if definition:
                states = await self.get_ha_states()
                if not states:
                    return False
                if entity_id not in {state.get("entity_id") for state in states}:
                    check_command_deadline(expires_at)
                    if not await self.call_ha_service("scene", "create", definition):
                        return False
            # HA lookups/restoration and another scene action may consume the
            # confirmation window. Never activate stale intent after preflight.
            check_command_deadline(expires_at)
            return await self.call_ha_service("scene", "turn_on", {"entity_id": entity_id})

    async def toggle_automation(self, entity_id: str, enable: bool) -> bool:
        """Enable or disable an automation."""
        service = "turn_on" if enable else "turn_off"
        return await self.call_ha_service("automation", service, {"entity_id": entity_id})

    async def _write_automation_config(self, automation_id: str, config=None, expires_at=None) -> bool:
        """Use HA's admin REST config API; acceptance does not prove reload completion.

        Contract: Home Assistant core 2026.10.4 components/config/{automation,view}.py.
        Never retry uncertain mutations here: the durable command journal owns replay.
        """
        if (not isinstance(automation_id, str) or not automation_id.startswith("lapahub_")
                or len(automation_id) > 160
                or not all(c in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in automation_id)):
            raise ValueError("Only explicit LapaHub automation configuration IDs may be changed")
        check_command_deadline(expires_at)
        request = self.session.delete if config is None else self.session.post
        kwargs = {
            "headers": {"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"},
            "timeout": aiohttp.ClientTimeout(total=30),
        }
        if config is not None:
            kwargs["json"] = config
        try:
            async with request(f"{HA_BASE_URL}/api/config/automation/config/{automation_id}", **kwargs) as resp:
                if resp.status != 200:
                    logger.error("HA automation config rejected (%s)", resp.status)
                    return False
                result = await resp.json()
                accepted = isinstance(result, dict) and result.get("result") == "ok"
                if accepted:
                    self.automation_refresh_wake.set()
                return accepted
        except asyncio.CancelledError:
            raise
        except Exception:
            # A response can be lost after HA has persisted the configuration.
            # Preserve uncertainty instead of recording a definitive rejection.
            logger.exception("HA automation configuration outcome uncertain")
            raise

    async def create_automation(self, automation_id: str, config: dict, expires_at=None) -> bool:
        """Persist a managed automation; HA schedules its reload asynchronously."""
        return await self._write_automation_config(automation_id, config, expires_at)

    async def delete_automation(self, automation_id: str, expires_at=None) -> bool:
        """Delete a managed automation through the same durable REST config API."""
        return await self._write_automation_config(automation_id, expires_at=expires_at)

    async def create_scene(self, scene_id: str, entities: dict = None, snapshot_entities: list = None, scene_name: str = None, expires_at=None) -> bool:
        """Create a dynamic scene and durably retain confirmed explicit states."""
        if scene_id.startswith("scene.") and not scene_id.startswith("scene.lapahub_"):
            raise ValueError("Imported HA scenes cannot be replaced by LapaHub")
        slug = scene_id.removeprefix("scene.").replace("-", "_")
        if not slug.startswith("lapahub_"):
            slug = "lapahub_" + slug
        service_data = {"scene_id": slug}
        if entities:
            service_data["entities"] = entities
        elif snapshot_entities:
            # Resolve snapshots once; a restart must not snapshot different states.
            states = await self.get_ha_states()
            by_id = {state["entity_id"]: state for state in states}
            if any(entity not in by_id for entity in snapshot_entities):
                return False
            service_data["entities"] = {
                entity: {**by_id[entity].get("attributes", {}), "state": by_id[entity]["state"]}
                for entity in snapshot_entities
            }
        else:
            return False
        async with self.scene_lock:
            check_command_deadline(expires_at)
            accepted = await self.call_ha_service("scene", "create", service_data)
            if accepted:
                self.managed_scenes.save("scene." + slug, service_data)
                self.log_activity(f"Created scene: {scene_name or scene_id}")
            return accepted

    async def delete_scene(self, scene_id: str, expires_at=None) -> bool:
        """Suppress persistent restoration before asking HA to delete a scene."""
        if scene_id.startswith("scene.") and not scene_id.startswith("scene.lapahub_"):
            raise ValueError("Imported HA scenes cannot be deleted by LapaHub")
        entity_id = scene_id if scene_id.startswith("scene.") else (
            "scene." + (scene_id if scene_id.startswith("lapahub_") else "lapahub_" + scene_id).replace("-", "_")
        )
        async with self.scene_lock:
            check_command_deadline(expires_at)
            self.managed_scenes.suppress(entity_id)
            return await self.call_ha_service("scene", "delete", {"entity_id": entity_id})

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

    async def fetch_device_registry(self) -> list:
        """Fetch HA device registry with manufacturer, model info via WebSocket.

        Returns list of physical devices with:
        - id: HA device_id
        - name: Device name
        - manufacturer: e.g., "Sungrow", "Philips"
        - model: e.g., "SH25T", "Hue Bridge"
        - sw_version: Firmware version
        - hw_version: Hardware version
        - serial_number: Device serial
        - via_device_id: Parent device (for nested devices)
        """
        try:
            # Device registry is only accessible via WebSocket, not REST API
            ws_url = f"{HA_BASE_URL.replace('http://', 'ws://').replace('https://', 'wss://')}/api/websocket"
            logger.info("Fetching device registry via WebSocket")

            async with self.session.ws_connect(ws_url) as ws:
                # Wait for auth_required message
                msg = await ws.receive_json()
                if msg.get("type") != "auth_required":
                    logger.warning(f"Unexpected WS message: {msg}")
                    return []

                # Send auth
                await ws.send_json({
                    "type": "auth",
                    "access_token": SUPERVISOR_TOKEN,
                })

                # Wait for auth response
                msg = await ws.receive_json()
                if msg.get("type") != "auth_ok":
                    logger.warning(f"WS auth failed: {msg}")
                    return []

                # Request device registry list
                await ws.send_json({
                    "id": 1,
                    "type": "config/device_registry/list",
                })

                # Wait for response
                msg = await ws.receive_json()
                if msg.get("success"):
                    devices = msg.get("result", [])
                    logger.info(f"Fetched {len(devices)} physical devices from HA device registry")
                    return devices
                else:
                    logger.error(f"Device registry request failed: {msg}")
                    return []

        except Exception as e:
            logger.error(f"Error getting HA device registry: {e}")
            return []

    async def fetch_entity_registry(self) -> dict:
        """Fetch HA entity registry to get entity_id → device_id mapping via WebSocket.

        Returns dict mapping entity_id to device_id.
        """
        try:
            # Entity registry is only accessible via WebSocket, not REST API
            ws_url = f"{HA_BASE_URL.replace('http://', 'ws://').replace('https://', 'wss://')}/api/websocket"
            logger.info("Fetching entity registry via WebSocket")

            async with self.session.ws_connect(ws_url) as ws:
                # Wait for auth_required message
                msg = await ws.receive_json()
                if msg.get("type") != "auth_required":
                    logger.warning(f"Unexpected WS message: {msg}")
                    return {}

                # Send auth
                await ws.send_json({
                    "type": "auth",
                    "access_token": SUPERVISOR_TOKEN,
                })

                # Wait for auth response
                msg = await ws.receive_json()
                if msg.get("type") != "auth_ok":
                    logger.warning(f"WS auth failed: {msg}")
                    return {}

                # Request entity registry list
                await ws.send_json({
                    "id": 1,
                    "type": "config/entity_registry/list",
                })

                # Wait for response
                msg = await ws.receive_json()
                if msg.get("success"):
                    entities = msg.get("result", [])
                    # Build entity_id → device_id mapping
                    entity_device_map = {}
                    for entity in entities:
                        entity_id = entity.get("entity_id")
                        device_id = entity.get("device_id")
                        if entity_id and device_id:
                            entity_device_map[entity_id] = device_id
                    logger.info(f"Fetched entity registry: {len(entity_device_map)} entities with device_id")
                    return entity_device_map
                else:
                    logger.error(f"Entity registry request failed: {msg}")
                    return {}

        except Exception as e:
            logger.error(f"Error getting HA entity registry: {e}")
            return {}

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
        """Fetch Energy Dashboard preferences from Home Assistant via WebSocket API."""
        try:
            # Use WebSocket API to get energy preferences
            ws_url = f"{HA_BASE_URL.replace('http://', 'ws://').replace('https://', 'wss://')}/api/websocket"
            logger.info(f"Fetching Energy Dashboard config via WebSocket")

            async with self.session.ws_connect(ws_url) as ws:
                # Wait for auth_required message
                msg = await ws.receive_json()
                if msg.get("type") != "auth_required":
                    logger.warning(f"Unexpected WS message: {msg}")
                    return False

                # Send auth
                await ws.send_json({
                    "type": "auth",
                    "access_token": SUPERVISOR_TOKEN,
                })

                # Wait for auth response
                msg = await ws.receive_json()
                if msg.get("type") != "auth_ok":
                    logger.warning(f"WS auth failed: {msg}")
                    return False

                # Request energy preferences
                await ws.send_json({
                    "id": 1,
                    "type": "energy/get_prefs",
                })

                # Wait for response
                msg = await ws.receive_json()
                if msg.get("success"):
                    self.energy_prefs = msg.get("result", {})
                    self.energy_prefs_last_fetch = datetime.now(timezone.utc)
                    sources = self.energy_prefs.get("energy_sources", [])
                    logger.info(f"Loaded Energy Dashboard config with {len(sources)} sources")
                    if sources:
                        self.log_activity(f"Energy Dashboard: {len(sources)} sources configured")
                        # Log the source types for debugging
                        for source in sources[:5]:  # Log first 5
                            source_type = source.get("type", "unknown")
                            stat_id = source.get("stat_energy_from") or source.get("stat_energy_to") or "N/A"
                            logger.info(f"  Energy source: {source_type} -> {stat_id}")
                    else:
                        logger.warning("Energy Dashboard has no sources configured")
                    return True
                else:
                    logger.warning(f"Energy prefs request failed: {msg}")
                    return False

        except Exception as e:
            logger.warning(f"Could not fetch energy preferences: {e}")
            return False

    async def device_sync_loop(self):
        """Periodically sync devices from HA to cloud."""
        logger.info(f"Starting device sync loop (interval: {self.sync_interval}s)")
        consecutive_errors = 0

        while self.running:
            try:
                # Sync physical devices first (from device registry)
                await self.sync_physical_devices()
                # Then sync entities (with parent device references)
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

    async def fetch_area_registry(self) -> dict:
        """Fetch HA area registry via WebSocket. Returns {area_id: area_name} lookup."""
        try:
            ws_url = f"{HA_BASE_URL.replace('http://', 'ws://').replace('https://', 'wss://')}/api/websocket"
            async with self.session.ws_connect(ws_url) as ws:
                msg = await ws.receive_json()
                if msg.get("type") != "auth_required":
                    return {}
                await ws.send_json({"type": "auth", "access_token": SUPERVISOR_TOKEN})
                msg = await ws.receive_json()
                if msg.get("type") != "auth_ok":
                    return {}
                await ws.send_json({"id": 1, "type": "config/area_registry/list"})
                msg = await ws.receive_json()
                if msg.get("success"):
                    areas = msg.get("result", [])
                    lookup = {a["area_id"]: a.get("name", a["area_id"]) for a in areas}
                    logger.info(f"Fetched {len(lookup)} areas from HA area registry")
                    return lookup
                return {}
        except Exception as e:
            logger.warning(f"Could not fetch area registry: {e}")
            return {}

    async def sync_physical_devices(self):
        """Sync physical devices from HA device registry to cloud.

        Physical devices represent actual hardware (inverters, bridges, etc.)
        that contain multiple entities (sensors, switches).
        Also fetches entity registry to build entity→device mapping.
        """
        registry = await self.fetch_device_registry()

        if not registry:
            return

        # Fetch entity registry to get entity_id → device_id mapping
        self.entity_device_map = await self.fetch_entity_registry()

        # Fetch area registry to resolve area_id → area_name
        self.area_lookup = await self.fetch_area_registry()

        # Build physical devices list with relevant info
        physical_devices = []
        for device in registry:
            device_id = device.get("id")
            if not device_id:
                continue

            # Skip devices without useful identifying info
            name = device.get("name_by_user") or device.get("name") or ""
            manufacturer = device.get("manufacturer") or ""
            model = device.get("model") or ""

            # Skip if no name, manufacturer, or model
            if not name and not manufacturer and not model:
                continue

            physical_device = {
                "id": device_id,
                "name": name,
                "manufacturer": manufacturer,
                "model": model,
                "sw_version": device.get("sw_version"),
                "hw_version": device.get("hw_version"),
                "serial_number": device.get("serial_number"),
                "via_device_id": device.get("via_device_id"),
                "config_entries": device.get("config_entries", []),
                "identifiers": device.get("identifiers", []),
                "connections": device.get("connections", []),
                "area_id": device.get("area_id"),
                "area_name": self.area_lookup.get(device.get("area_id", ""), ""),
            }
            physical_devices.append(physical_device)
            self.physical_devices[device_id] = physical_device

        logger.info(f"Synced {len(physical_devices)} physical devices from registry")
        await self.push_physical_devices_to_cloud(physical_devices)

    async def push_physical_devices_to_cloud(self, devices: list):
        """Push physical devices list to LapaHub cloud."""
        if not self.firebase_credentials:
            logger.debug("Skipping physical device push (no credentials)")
            return

        api_url = f"https://us-central1-{self.firebase_project}.cloudfunctions.net/syncPhysicalDevices"

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
                    logger.debug(f"Pushed {len(devices)} physical devices to cloud")
                else:
                    logger.warning(f"Physical device push failed: {resp.status}")
        except Exception as e:
            logger.warning(f"Could not push physical devices to cloud: {e}")

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
        entities_with_parent = 0
        for state in states:
            entity_id = state.get("entity_id", "")
            domain = entity_id.split(".")[0] if "." in entity_id else ""

            if domain not in relevant_domains:
                continue

            attributes = state.get("attributes", {})

            # Get device_id from entity registry mapping (populated during sync_physical_devices)
            device_id = self.entity_device_map.get(entity_id)

            # Get parent device info if we have it cached
            parent_device_name = None
            area_id = None
            if device_id and device_id in self.physical_devices:
                parent_device = self.physical_devices[device_id]
                parent_device_name = parent_device.get("name")
                area_id = parent_device.get("area_id")

            device = {
                "entity_id": entity_id,
                "domain": domain,
                "friendly_name": attributes.get("friendly_name", entity_id),
                "device_class": attributes.get("device_class"),
                "state": state.get("state"),
                "attributes": attributes,
                "last_updated": state.get("last_updated"),
                # Parent device linkage (new fields for hierarchical view)
                "parent_device_id": device_id,
                "parent_device_name": parent_device_name,
                # HA area for room auto-creation
                "area_id": area_id,
                "area_name": self.area_lookup.get(area_id or "", "") if hasattr(self, 'area_lookup') else "",
            }
            devices.append(device)

            if device_id:
                entities_with_parent += 1

        self.devices = {d["entity_id"]: d for d in devices}
        logger.info(f"Synced {len(devices)} entities ({entities_with_parent} with parent device)")

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
        """Listen for commands via SSE streaming (Cloud Functions v2).

        Opens a persistent HTTP connection to the waitForCommands endpoint.
        The server pushes command events in real-time via Server-Sent Events.
        Commands arrive in <500ms — no polling, no wasted cycles.

        Falls back to classic 1s poll if SSE endpoint unavailable.
        """
        logger.info("Starting command listener (SSE streaming mode)")
        consecutive_errors = 0
        use_sse = True

        while self.running:
            try:
                if use_sse:
                    await self._stream_commands_sse()
                else:
                    await self._poll_commands_classic()
                    await asyncio.sleep(1)
                # SSE returns normally when stream ends — reconnect immediately
                consecutive_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors <= 3:
                    logger.error(f"Error in command listener: {e}")

                if consecutive_errors >= 3 and use_sse:
                    logger.warning("SSE failing, falling back to classic 1s poll")
                    use_sse = False
                    consecutive_errors = 0

                if consecutive_errors >= 5:
                    self.log_activity("Too many command errors, re-authenticating", "warning")
                    await self.authenticate_with_retry()
                    use_sse = True
                    consecutive_errors = 0

                await asyncio.sleep(1)

    async def _stream_commands_sse(self):
        """Open persistent SSE connection to receive commands in real-time."""
        if not self.firebase_credentials:
            await asyncio.sleep(5)
            return

        api_url = f"https://us-central1-{self.firebase_project}.cloudfunctions.net/waitForCommands"

        async with self.session.get(
            api_url,
            params={"hubId": self.hub_id, "commandProtocol": "2"},
            headers={
                "Authorization": f"Bearer {self.firebase_credentials.get('token', '')}",
                "Accept": "text/event-stream",
            },
            timeout=aiohttp.ClientTimeout(
                total=130,     # Server timeout is 120s, add margin
                sock_read=45,  # Heartbeat every 15s, so 45s without data = dead
            ),
        ) as resp:
            if resp.status == 401:
                raise Exception("Unauthorized — token may have expired")
            if resp.status != 200:
                raise Exception(f"waitForCommands returned {resp.status}")

            logger.info("SSE command stream connected")
            self.log_activity("Command stream connected (SSE)")

            # Parse SSE events line-by-line from the response stream
            event_type = None
            data_lines = []

            while self.running:
                line_bytes = await resp.content.readline()
                if not line_bytes:
                    break  # Stream closed

                line = line_bytes.decode("utf-8").rstrip("\n").rstrip("\r")

                if line.startswith("event: "):
                    event_type = line[7:]
                elif line.startswith("data: "):
                    data_lines.append(line[6:])
                elif line == "":
                    # Empty line = end of SSE event
                    if event_type and data_lines:
                        data_str = "\n".join(data_lines)
                        await self._handle_sse_event(event_type, data_str)
                    event_type = None
                    data_lines = []

            logger.info("SSE stream ended, will reconnect")

    async def _handle_sse_event(self, event_type: str, data_str: str):
        """Process a single SSE event from the command stream."""
        if event_type == "commands":
            try:
                payload = json.loads(data_str)
                commands = payload.get("commands", [])
                if commands:
                    logger.info(f"SSE: received {len(commands)} command(s)")
                    received_at = datetime.now(timezone.utc).timestamp()
                    for cmd in commands:
                        cmd['_received_at'] = received_at
                        logger.info("Command received id=%s intent=%s", cmd.get('id'), cmd.get('clientIntentId'))
                    for cmd in commands:
                        await self.execute_command(cmd)
            except json.JSONDecodeError as e:
                logger.warning(f"SSE: invalid JSON in commands event: {e}")
        elif event_type == "connected":
            logger.debug("SSE: connected event received")
        elif event_type == "heartbeat":
            pass  # Keep-alive, no action needed
        elif event_type == "error":
            logger.warning("SSE: server returned an error event")
            raise Exception("SSE server returned an error event")
        else:
            logger.debug(f"SSE: unknown event type '{event_type}'")

    async def _poll_commands_classic(self):
        """Classic poll fallback — single GET for pending commands."""
        if not self.firebase_credentials:
            return

        api_url = f"https://us-central1-{self.firebase_project}.cloudfunctions.net/getPendingCommands"

        async with self.session.get(
            api_url,
            params={"hubId": self.hub_id, "commandProtocol": "2"},
            headers={"Authorization": f"Bearer {self.firebase_credentials.get('token', '')}"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                commands = data.get("commands", [])
                received_at = datetime.now(timezone.utc).timestamp()
                for cmd in commands:
                    cmd['_received_at'] = received_at
                    logger.info("Command received id=%s intent=%s", cmd.get('id'), cmd.get('clientIntentId'))
                for cmd in commands:
                    await self.execute_command(cmd)

    async def execute_command(self, command: dict):
        cmd_id = command.get("id")
        claimed = False
        try:
            if not self.command_journal.claim(command):
                logger.info("Duplicate command %s suppressed", cmd_id)
                return
            claimed = True
            check_command_deadline(command.get("expires_at"))
            await self._execute_command_once(command)
        except CommandExpired:
            if claimed:
                self.command_journal.complete(cmd_id, "expired")
            logger.warning("Expired command %s rejected", cmd_id)
        except asyncio.CancelledError:
            # Keep the pre-dispatch receipt durable; restart resolves it as unknown.
            raise
        except Exception:
            logger.exception("Command %s could not complete", cmd_id)
            # Do not blindly retry a command whose physical effect is uncertain.
            if claimed:
                self.command_journal.complete(cmd_id, "unknown")

    async def _execute_command_once(self, command: dict):
        """Execute a command from the cloud.

        Handles both standard device commands and special automation/scene commands:
        - automation.create: Create a new automation in HA
        - automation.update: Update an existing automation
        - automation.delete: Delete an automation
        - scene.create: Create a new scene in HA
        - scene.update: Update an existing scene
        - scene.delete: Delete a scene
        - scene.activate: Activate a scene
        - automation.trigger: Manually trigger an automation
        - automation.toggle: Enable/disable an automation
        """
        cmd_id = command.get("id")
        params = command.get("params", {})
        entity_id = command.get("entity_id") or params.get("entity_id", "")
        action = command.get("action")

        logger.info(f"Executing command {cmd_id}: {action} on {entity_id or 'N/A'}")
        success = False

        # Handle special automation/scene commands
        if action == "automation.create":
            # Create new automation
            automation_id = params.get("automation_id")
            config = params.get("config", {})
            if automation_id and config:
                success = await self.create_automation(automation_id, config, expires_at=command.get("expires_at"))
            else:
                logger.error("automation.create requires automation_id and config in params")

        elif action == "automation.update":
            # Update existing automation (same as create in HA)
            automation_id = params.get("automation_id")
            config = params.get("config", {})
            if automation_id and config:
                success = await self.create_automation(automation_id, config, expires_at=command.get("expires_at"))
            else:
                logger.error("automation.update requires automation_id and config in params")

        elif action == "automation.delete":
            # Delete an automation
            automation_id = params.get("automation_id")
            if automation_id:
                success = await self.delete_automation(automation_id, expires_at=command.get("expires_at"))
            else:
                logger.error("automation.delete requires automation_id in params")

        elif action == "automation.trigger":
            # Manually trigger an automation
            if entity_id:
                success = await self.call_ha_service("automation", "trigger", {"entity_id": entity_id, "skip_condition": False})
            else:
                logger.error("automation.trigger requires entity_id")

        elif action == "automation.toggle":
            # Enable/disable automation
            if entity_id:
                enable = params.get("enabled", True)
                success = await self.toggle_automation(entity_id, enable)
            else:
                logger.error("automation.toggle requires entity_id")

        elif action == "scene.create":
            # Create new scene
            # Cloud Function sends: scene_id, entities (dict), snapshot_entities (list)
            scene_id = params.get("scene_id")
            entities = params.get("entities")
            snapshot_entities = params.get("snapshot_entities")
            scene_name = params.get("name") or params.get("friendly_name")

            if scene_id and (entities or snapshot_entities):
                success = await self.create_scene(
                    scene_id=scene_id,
                    entities=entities,
                    snapshot_entities=snapshot_entities,
                    scene_name=scene_name,
                    expires_at=command.get("expires_at"),
                )
            else:
                logger.error("scene.create requires scene_id and either entities or snapshot_entities")

        elif action == "scene.update":
            # Update existing scene (same as create in HA - just recreate with new entities)
            scene_id = params.get("scene_id")
            entities = params.get("entities")
            snapshot_entities = params.get("snapshot_entities")
            scene_name = params.get("name") or params.get("friendly_name")

            if scene_id and (entities or snapshot_entities):
                success = await self.create_scene(
                    scene_id=scene_id,
                    entities=entities,
                    snapshot_entities=snapshot_entities,
                    scene_name=scene_name,
                    expires_at=command.get("expires_at"),
                )
            else:
                logger.error("scene.update requires scene_id and either entities or snapshot_entities")

        elif action == "scene.delete":
            # Delete a scene
            scene_id = params.get("scene_id") or params.get("entity_id")
            if scene_id:
                success = await self.delete_scene(scene_id, expires_at=command.get("expires_at"))
            else:
                logger.error("scene.delete requires scene_id in params")

        elif action in ("scene.activate", "scene.turn_on"):
            # Activate a scene
            if entity_id:
                success = await self.trigger_scene(entity_id, expires_at=command.get("expires_at"))
            else:
                logger.error("scene.activate requires entity_id")

        else:
            # Standard device command - map to HA service call
            domain = entity_id.split(".")[0] if entity_id and "." in entity_id else "homeassistant"

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
            if isinstance(service, str) and "." in service:
                action_domain, service = service.split(".", 1)
                if action_domain not in (domain, "homeassistant"):
                    raise ValueError("Command domain does not match target entity")
            service_data = {**params, "entity_id": entity_id}

            success = await self.call_ha_service(domain, service, service_data)

        # Report command result back to cloud
        self.command_journal.complete(cmd_id, "service_accepted" if success else "unknown")

    async def report_command_result(self, cmd_id: str, outcome: str):
        """Return only after a successful HTTP acknowledgement."""
        if not self.firebase_credentials:
            raise RuntimeError("No cloud session for acknowledgement")
        api_url = f"https://us-central1-{self.firebase_project}.cloudfunctions.net/reportCommandResult"
        async with self.session.post(
            api_url,
            json={"commandId": cmd_id, "hubId": self.hub_id,
                  "success": outcome == "service_accepted", "outcome": outcome,
                  "trace": self.command_journal.trace(cmd_id),
                  "timestamp": datetime.now(timezone.utc).isoformat()},
            headers={"Authorization": f"Bearer {self.firebase_credentials.get('token', '')}"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Acknowledgement HTTP {resp.status}")

    async def command_ack_loop(self):
        delay = 1
        while self.running:
            try:
                for cmd_id, outcome in self.command_journal.pending():
                    await self.report_command_result(cmd_id, outcome)
                    self.command_journal.acknowledge(cmd_id)
                delay = 1
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("Acknowledgement retained for retry: %s", error)
                delay = min(delay * 2, 30)
            await asyncio.sleep(delay)

    async def energy_report_loop(self):
        """Periodically report energy data to cloud."""
        logger.info(f"Starting energy report loop (interval: {self.energy_interval}s)")
        consecutive_errors = 0

        while self.running:
            try:
                await self.report_energy()
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
        # Retry the exact snapshot first: a delivery failure must not relabel old
        # measurements with a new observation time or silently discard them.
        pending = getattr(self, "pending_energy_report", None)
        if pending is None:
            if (not self.energy_prefs_last_fetch or
                    datetime.now(timezone.utc) - self.energy_prefs_last_fetch > timedelta(minutes=5)):
                await self.fetch_energy_prefs()
            states = await self.get_ha_states()
            if not states:
                raise RuntimeError("No Home Assistant states available for energy report")
            pending = collect_energy_payload(states, self.energy_prefs or {})
            self.pending_energy_report = pending
        try:
            await self.push_energy_to_cloud(pending)
        except EnergyDeliveryError as error:
            if error.permanent:
                logger.error("Discarding permanently rejected energy snapshot: %s", error)
                self.pending_energy_report = None
            raise
        self.last_energy_report = datetime.fromisoformat(pending["timestamp"])
        self.pending_energy_report = None

    async def push_energy_to_cloud(self, energy_data: dict):
        """Push energy data to LapaHub cloud."""
        if not self.firebase_credentials or not self.firebase_credentials.get("token"):
            raise RuntimeError("Energy push requires authenticated credentials")
        api_url = f"https://us-central1-{self.firebase_project}.cloudfunctions.net/reportEnergy"
        async with self.session.post(
            api_url,
            json={"hubId": self.hub_id, **energy_data},
            headers={"Authorization": f"Bearer {self.firebase_credentials['token']}"},
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status != 200:
                raise EnergyDeliveryError(resp.status)
            logger.debug("Energy data pushed to cloud")

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
            "state_publication": {"queued": len(self.state_publisher.pending),
                                  "active": len(self.state_publisher.active),
                                  "coalesced": self.state_publisher.coalesced,
                                  "dropped": self.state_publisher.dropped},
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
