# Changelog

## 1.0.49

- Fix SSE stream parsing — use readline() instead of chunk iteration
- Commands were not being received because aiohttp chunk reader doesn't split on newlines

## 1.0.48

- SSE streaming for instant command delivery (<200ms vs 1-5s polling)
- Persistent connection to Cloud Functions v2 — zero polling overhead
- Automatic fallback to classic poll if SSE endpoint unavailable
- Heartbeat keep-alive with dead-connection detection

## 1.0.47

- Rooms auto-creation from HA area registry
- Area ID and area name sent with device sync payloads

## 1.0.46

- Read addon version from HA Supervisor API — single source of truth
- No more hardcoded version constant in run.py
- Version format: `addon:ha` (e.g., `1.0.46:2026.3.1`) for troubleshooting

## 1.0.45

- Test auto version read from config.yaml (superseded by Supervisor API in 1.0.46)

## 1.0.44

- Sync ADDON_VERSION with config.yaml
- Restored addon:ha version format for firmware reporting

## 1.0.43

- Send area_id and area_name with device sync payloads
- Fetch HA area registry via WebSocket for room auto-creation
- Physical devices include area_id for room assignment
- Entity sync includes area_id and area_name from parent device

## 1.0.42

- Fix UnboundLocalError in energy_report_loop grid processing
- Initialize power_sensor variable before conditional block
- Fixes energy data not flowing to cloud (reportEnergy never called)

## 1.0.41

- Energy Dashboard config integration for sensor mapping
- Sensor realtime mode (beta) with configurable poll interval
- Improved energy report with dashboard_sources
- Grid source config parsing with flow_from/flow_to support

## 1.0.40

- Token refresh mechanism (LAPA-71)
- Automatic reconnection logic with exponential backoff (LAPA-72)
- Graceful shutdown handling for SIGTERM/SIGINT (LAPA-73)

## 1.0.39

- Real-time state updates via HA WebSocket (LAPA-74)
- Health heartbeat to cloud with CPU, memory, uptime metrics (LAPA-75)
- Error recovery in sync loops — auto re-auth after consecutive failures (LAPA-76)

## 1.0.38

- Web UI dashboard improvements with activity log (LAPA-77)
- Sync scenes and automations to cloud (LAPA-78)
- Automation config fetching via WebSocket

## 1.0.33-1.0.37

- Incremental bug fixes and stability improvements
- Enhanced device sync reliability
- Improved error handling and logging

## 1.0.32

- Fix: Use WebSocket API for device/entity registry (REST API returns 404)
- Physical devices now correctly sync with manufacturer, model, and firmware info
- Entity→device mapping now works for hierarchical device grouping

## 1.0.31

- Add parent device architecture support
- Sync physical devices (inverters, hubs, bridges) to cloud
- Link entities to parent devices for hierarchical view

## 1.0.30

- Use Home Assistant Energy Dashboard config for sensor mapping
- Improved energy flow calculation accuracy

## 1.0.0

- Initial release
- Device discovery and sync to LapaHub cloud
- Command execution from mobile app
- Energy sensor data reporting
- Web-based status interface
