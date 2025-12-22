#!/usr/bin/env python3
"""
LapaHub Home Assistant Addon

Connects Home Assistant to LapaHub cloud for smart energy management.
- Syncs devices to Firebase
- Receives commands from cloud
- Reports energy data
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

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
OPTIONS_PATH = Path("/data/options.json")
SUPERVISOR_TOKEN_PATH = Path("/run/supervisor/token")
HA_BASE_URL = "http://supervisor/core"

def get_supervisor_token() -> str | None:
    """Get supervisor token from environment or file."""
    # Try environment variable first
    token = os.environ.get("SUPERVISOR_TOKEN")
    if token:
        logger.info("Got SUPERVISOR_TOKEN from environment")
        return token
    # Try reading from file (for init: false mode)
    logger.info(f"Token file exists: {SUPERVISOR_TOKEN_PATH.exists()}")
    if SUPERVISOR_TOKEN_PATH.exists():
        token = SUPERVISOR_TOKEN_PATH.read_text().strip()
        logger.info(f"Got token from file, length: {len(token)}")
        return token
    # List /run/supervisor to debug
    supervisor_path = Path("/run/supervisor")
    if supervisor_path.exists():
        logger.info(f"Contents of /run/supervisor: {list(supervisor_path.iterdir())}")
    else:
        logger.info("/run/supervisor does not exist")
    return None

SUPERVISOR_TOKEN = get_supervisor_token()


class LapaHubAddon:
    """Main addon class that manages all LapaHub functionality."""

    def __init__(self):
        self.options = self._load_options()
        self.hub_id = self.options.get("hub_id", "")
        self.api_key = self.options.get("api_key", "")
        self.sync_interval = self.options.get("sync_interval_seconds", 60)
        self.energy_interval = self.options.get("energy_report_interval_seconds", 300)
        self.firebase_project = os.environ.get("FIREBASE_PROJECT_ID", "lapahub-dev-c8872")

        self.session: aiohttp.ClientSession | None = None
        self.devices: dict = {}
        self.running = True

        # Firebase credentials from API key exchange
        self.firebase_credentials = None

    def _load_options(self) -> dict:
        """Load addon options from config."""
        if OPTIONS_PATH.exists():
            with open(OPTIONS_PATH) as f:
                return json.load(f)
        return {}

    async def start(self):
        """Start the addon."""
        logger.info("Starting LapaHub Addon v1.0.8")
        logger.info(f"Hub ID: {self.hub_id}")
        logger.info(f"Supervisor token present: {bool(SUPERVISOR_TOKEN)}")
        logger.info(f"Supervisor token length: {len(SUPERVISOR_TOKEN) if SUPERVISOR_TOKEN else 0}")

        if not self.hub_id or not self.api_key:
            logger.error("Hub ID and API Key must be configured in addon options")
            return

        if not SUPERVISOR_TOKEN:
            logger.warning("SUPERVISOR_TOKEN not found - HA API access will fail")

        # Create aiohttp session
        self.session = aiohttp.ClientSession()

        try:
            # Authenticate with LapaHub cloud
            await self.authenticate()

            # Start background tasks
            await asyncio.gather(
                self.device_sync_loop(),
                self.command_listener_loop(),
                self.energy_report_loop(),
                self.run_web_server(),
            )
        except Exception as e:
            logger.exception(f"Fatal error: {e}")
        finally:
            if self.session:
                await self.session.close()

    async def authenticate(self):
        """Authenticate with LapaHub cloud and get Firebase credentials."""
        logger.info(f"Authenticating hub {self.hub_id} with LapaHub cloud...")

        # Exchange API key for Firebase credentials
        # In production, this would call a Cloud Function
        api_url = f"https://us-central1-{self.firebase_project}.cloudfunctions.net/authenticateHub"

        try:
            async with self.session.post(
                api_url,
                json={"hubId": self.hub_id, "apiKey": self.api_key},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    self.firebase_credentials = await resp.json()
                    logger.info("Successfully authenticated with LapaHub cloud")
                else:
                    error = await resp.text()
                    logger.error(f"Authentication failed: {error}")
                    # For development, continue without cloud auth
                    logger.warning("Continuing in offline mode for development")
        except aiohttp.ClientError as e:
            logger.warning(f"Could not reach LapaHub cloud: {e}")
            logger.warning("Continuing in offline mode for development")

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

    async def device_sync_loop(self):
        """Periodically sync devices from HA to cloud."""
        logger.info(f"Starting device sync loop (interval: {self.sync_interval}s)")

        while self.running:
            try:
                await self.sync_devices()
            except Exception as e:
                logger.error(f"Error in device sync: {e}")

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

        while self.running:
            try:
                await self.poll_commands()
            except Exception as e:
                logger.error(f"Error polling commands: {e}")

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

        while self.running:
            try:
                await self.report_energy()
            except Exception as e:
                logger.error(f"Error reporting energy: {e}")

            await asyncio.sleep(self.energy_interval)

    async def report_energy(self):
        """Collect and report energy data."""
        states = await self.get_ha_states()

        if not states:
            return

        energy_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sensors": {},
        }

        # Collect energy-related sensors
        energy_domains = ["sensor"]
        energy_classes = [
            "energy", "power", "voltage", "current",
            "battery", "power_factor", "frequency",
        ]

        for state in states:
            entity_id = state.get("entity_id", "")
            domain = entity_id.split(".")[0]

            if domain not in energy_domains:
                continue

            attributes = state.get("attributes", {})
            device_class = attributes.get("device_class")

            if device_class in energy_classes:
                try:
                    value = float(state.get("state", 0))
                    energy_data["sensors"][entity_id] = {
                        "value": value,
                        "unit": attributes.get("unit_of_measurement"),
                        "device_class": device_class,
                        "friendly_name": attributes.get("friendly_name"),
                    }
                except (ValueError, TypeError):
                    pass

        if energy_data["sensors"]:
            logger.info(f"Collected {len(energy_data['sensors'])} energy readings")
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

        runner = web.AppRunner(app)
        await runner.setup()

        site = web.TCPSite(runner, "0.0.0.0", 8099)
        await site.start()

        logger.info("Web interface started on port 8099")

        # Keep running
        while self.running:
            await asyncio.sleep(3600)

    async def handle_index(self, request):
        """Serve the main web interface."""
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>LapaHub Bridge</title>
            <style>
                body { font-family: -apple-system, system-ui, sans-serif; margin: 40px; background: #f5f5f5; }
                .container { max-width: 800px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
                h1 { color: #1a73e8; margin-bottom: 10px; }
                .status { padding: 15px; border-radius: 5px; margin: 20px 0; }
                .status.connected { background: #e6f4ea; color: #137333; }
                .status.disconnected { background: #fce8e6; color: #c5221f; }
                .stat { display: inline-block; padding: 10px 20px; background: #e8f0fe; border-radius: 5px; margin: 5px; }
                .stat-value { font-size: 24px; font-weight: bold; color: #1a73e8; }
                .stat-label { font-size: 12px; color: #666; }
            </style>
        </head>
        <body>
            <div class="container">
                <h1>LapaHub Bridge</h1>
                <p>Connecting Home Assistant to LapaHub Cloud</p>

                <div class="status connected" id="status">
                    Connected to Home Assistant
                </div>

                <div>
                    <div class="stat">
                        <div class="stat-value" id="device-count">-</div>
                        <div class="stat-label">Devices Synced</div>
                    </div>
                    <div class="stat">
                        <div class="stat-value" id="hub-id">-</div>
                        <div class="stat-label">Hub ID</div>
                    </div>
                </div>

                <h3>Recent Activity</h3>
                <div id="activity">Loading...</div>
            </div>

            <script>
                async function updateStatus() {
                    try {
                        const resp = await fetch('/api/status');
                        const data = await resp.json();
                        document.getElementById('device-count').textContent = data.device_count;
                        document.getElementById('hub-id').textContent = data.hub_id || 'Not configured';
                        document.getElementById('activity').textContent = 'Last sync: ' + (data.last_sync || 'Never');
                    } catch(e) {
                        console.error(e);
                    }
                }
                updateStatus();
                setInterval(updateStatus, 10000);
            </script>
        </body>
        </html>
        """
        return web.Response(text=html, content_type="text/html")

    async def handle_status(self, request):
        """Return current status."""
        return web.json_response({
            "hub_id": self.hub_id,
            "device_count": len(self.devices),
            "connected": bool(SUPERVISOR_TOKEN),
            "cloud_authenticated": bool(self.firebase_credentials),
            "last_sync": datetime.now(timezone.utc).isoformat(),
        })

    async def handle_devices(self, request):
        """Return synced devices."""
        return web.json_response({"devices": list(self.devices.values())})


async def main():
    """Main entry point."""
    addon = LapaHubAddon()
    await addon.start()


if __name__ == "__main__":
    asyncio.run(main())
