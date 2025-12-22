# LapaHub Bridge Addon

This addon connects your Home Assistant to LapaHub cloud for smart energy management and remote device control.

## Features

- **Device Sync**: Automatically discovers and syncs all your Home Assistant devices to LapaHub cloud
- **Remote Control**: Control your devices from the LapaHub mobile app, anywhere in the world
- **Energy Monitoring**: Reports energy consumption data for AI-powered optimization
- **Load Shedding Awareness**: Receive automated device control during Eskom load shedding
- **Web Dashboard**: Built-in status page accessible via Home Assistant sidebar
- **Offline Mode**: Continues local operation if cloud connection is lost

## How It Works

```
┌─────────────────────────────────────────────────────────────┐
│                    Home Assistant                            │
│                  (Your Smart Devices)                        │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                  LapaHub Bridge Addon                        │
│                                                              │
│  • Syncs devices every 60s (configurable)                   │
│  • Polls for commands every 5s                              │
│  • Reports energy data every 5 minutes                      │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼ (Outbound HTTPS only)
┌─────────────────────────────────────────────────────────────┐
│                    LapaHub Cloud                             │
│               (Firebase Cloud Functions)                     │
│                                                              │
│  • Stores device state                                       │
│  • Queues commands from mobile app                          │
│  • Processes energy data for AI optimization                │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                   LapaHub Mobile App                         │
│                                                              │
│  • View and control devices                                  │
│  • Monitor energy usage                                      │
│  • Receive AI recommendations                                │
└─────────────────────────────────────────────────────────────┘
```

**Security Note**: The addon only makes outbound connections. No ports are exposed to the internet. All communication is over HTTPS.

## Installation

### Step 1: Add Repository

1. Open Home Assistant
2. Go to **Settings** → **Add-ons** → **Add-on Store**
3. Click the three dots (⋮) in the top right corner
4. Select **Repositories**
5. Add this URL: `https://github.com/skhokho/lapahub-addon`
6. Click **Add** → **Close**

### Step 2: Install Addon

1. Refresh the Add-on Store page
2. Scroll down to find **LapaHub Bridge** under the LapaHub repository
3. Click on it, then click **Install**
4. Wait for installation to complete

### Step 3: Configure

1. Go to the **Configuration** tab
2. Enter your **Hub ID** and **API Key** (see below)
3. Adjust sync intervals if needed
4. Click **Save**

### Step 4: Start

1. Go to the **Info** tab
2. Toggle **Start on boot** (recommended)
3. Click **Start**
4. Check the **Log** tab for status

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `hub_id` | (required) | Your unique Hub ID from the LapaHub app |
| `api_key` | (required) | Your API key for secure authentication |
| `sync_interval_seconds` | `60` | How often to sync device states (30-3600 seconds) |
| `energy_report_interval_seconds` | `300` | How often to report energy data (60-3600 seconds) |
| `log_level` | `info` | Logging verbosity: `debug`, `info`, `warning`, `error` |

### Recommended Settings

- **Standard use**: Keep defaults (60s sync, 300s energy)
- **Faster response**: Set `sync_interval_seconds` to `30`
- **Lower bandwidth**: Set `sync_interval_seconds` to `120` and `energy_report_interval_seconds` to `600`
- **Debugging issues**: Set `log_level` to `debug`

## Getting Your Hub ID and API Key

### From LapaHub Mobile App

1. Open the **LapaHub** app on your phone
2. Tap the menu icon and go to **Settings**
3. Select **Hub Settings**
4. Tap **Connect Home Assistant**
5. Your **Hub ID** and **API Key** will be displayed
6. Copy these values to the addon configuration

### From Technician Installation

If a LapaHub technician installed your system:

1. Check your installation documentation
2. Or contact LapaHub support with your customer ID

## Supported Devices

The addon syncs devices from these Home Assistant domains:

| Domain | Examples |
|--------|----------|
| `switch` | Smart plugs, outlets |
| `light` | Bulbs, LED strips, dimmers |
| `climate` | Thermostats, air conditioners |
| `cover` | Blinds, garage doors |
| `fan` | Ceiling fans, ventilation |
| `sensor` | Temperature, humidity, energy monitors |
| `binary_sensor` | Motion, door/window sensors |
| `lock` | Smart locks |
| `media_player` | TVs, speakers |

### Energy Sensors

For AI optimization, the addon specifically tracks sensors with these device classes:
- `energy` - Energy consumption (kWh)
- `power` - Power draw (W)
- `voltage` - Voltage readings (V)
- `current` - Current readings (A)
- `battery` - Battery level (%)

## Web Interface

The addon includes a built-in web interface accessible from the Home Assistant sidebar.

### Accessing the Dashboard

1. After starting the addon, click **Open Web UI** on the addon page
2. Or click **LapaHub** in the Home Assistant sidebar

### Dashboard Features

- **Connection Status**: Shows if connected to Home Assistant and LapaHub cloud
- **Device Count**: Number of devices currently synced
- **Hub ID**: Your configured hub identifier
- **Last Sync**: Timestamp of most recent device sync

### API Endpoints

For advanced users, the addon exposes local API endpoints:

- `GET /api/status` - Current addon status (JSON)
- `GET /api/devices` - List of synced devices (JSON)

## Data & Privacy

### What Data is Sent to LapaHub Cloud

| Data Type | Purpose | Frequency |
|-----------|---------|-----------|
| Device list | Remote control from app | Every sync interval |
| Device states | Real-time status in app | Every sync interval |
| Entity names | Display in app | Every sync interval |
| Energy readings | AI optimization | Every energy interval |

### What is NOT Sent

- Home Assistant login credentials
- Your home's location/address
- Camera feeds or recordings
- Personal data beyond device names

### Data Storage

- All data is stored in Google Cloud (Firebase)
- Data is encrypted in transit (TLS) and at rest
- You can request data deletion by contacting support

## Troubleshooting

### Addon Won't Start

**Check logs for errors:**
1. Go to addon **Log** tab
2. Look for red error messages
3. Common issues:
   - Missing Hub ID or API Key → Configure in **Configuration** tab
   - Network issues → Check Home Assistant internet connection

### Devices Not Syncing

**Verify Home Assistant integration:**
1. Set `log_level` to `debug`
2. Restart the addon
3. Check logs for "Synced X devices" messages
4. If 0 devices, check Home Assistant has devices configured

**Device domain not supported:**
- Only domains listed in "Supported Devices" are synced
- Custom integrations may not be detected

### Can't Control Devices from App

**Check command execution:**
1. Set `log_level` to `debug`
2. Try controlling a device from the app
3. Look for "Executing command" in logs

**Common issues:**
- Cloud authentication failed → Check API key is correct
- Service call failed → Device may be offline or unresponsive

### Energy Data Not Showing

**Verify energy sensors exist:**
1. Go to Home Assistant → Developer Tools → States
2. Filter by `sensor.`
3. Look for sensors with `device_class: energy` or `device_class: power`

**No energy sensors?**
- Install energy monitoring hardware (e.g., Shelly EM, CT clamps)
- Configure Home Assistant Energy Dashboard first

### Cloud Connection Issues

**"Continuing in offline mode" message:**
- This is normal during development or if cloud is unreachable
- Check internet connection
- Verify Hub ID and API Key are correct
- LapaHub cloud services may be under maintenance

## FAQ

**Q: Does this work without internet?**
A: Home Assistant continues to work locally. The addon will queue data and sync when connection is restored.

**Q: Can I use this without the mobile app?**
A: The addon is designed to work with the LapaHub mobile app. Without it, you won't receive the full benefits.

**Q: How do I update the addon?**
A: Go to the addon page and click **Update** when available. Or enable auto-update in addon settings.

**Q: Is my data secure?**
A: Yes. All communication uses HTTPS. No inbound ports are opened. Data is encrypted in Google Cloud.

**Q: Can technicians see my data?**
A: Technicians only see device lists during authorized service visits. They cannot access historical data or control devices without your permission.

**Q: What happens if I change my Home Assistant password?**
A: The addon uses a Supervisor token, not your password. Changing your password won't affect the addon.

## Support

- **Issues**: [github.com/skhokho/lapahub-addon/issues](https://github.com/skhokho/lapahub-addon/issues)
- **Email**: support@lapahub.co.za
- **Documentation**: [lapahub.co.za/docs](https://lapahub.co.za/docs)
