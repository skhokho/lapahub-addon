# Changelog

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
