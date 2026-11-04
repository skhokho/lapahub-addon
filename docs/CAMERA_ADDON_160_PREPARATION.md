# Camera addon candidate on installed 1.0.60

This isolated candidate starts at `1eea828` (installed code `455f939` plus rollback documentation). It ports the historical camera API evidence and camera-only implementation without importing the uninstalled SSE transport candidate.

`camera_live_enabled` defaults to false. Missing/false disables camera discovery, realtime publication and relay polling before worker initialization. Camera controls and scene membership remain rejected regardless of this flag. Enabling requires the separate backend/privacy/ownership rollout gate; this branch does not enable any live installation.

The relay URL is fixed. Hub and Supervisor credentials appear only in their respective Authorization headers; redirects are disabled. Each valid demand permits at most four views, two concurrent captures, bounded metadata, 512 KiB JPEGs and a monotonic lease deadline. Capture has no database/frame cache; database schemas, original command deadlines, target preflight and first actual dispatch evidence remain unchanged. Requested dimensions are advisory HA query parameters, not a claim that every integration resizes.

Validation on this preparation: combined 120 Python tests passed, including missing/false gate no-contact checks, camera privacy/control/scene guards, simulated HTTP capture/expiry/revocation/JPEG bounds, existing 71 service mappings, preflight failures, multi-service dispatch evidence and both database reopen/retention tests. Python compile and whitespace checks passed. Image build/import/private-database smoke is a separate CI gate, not yet claimed by this document.

`HA_CAMERA_API_2026-10-29.*` records historical generated isolated HA evidence; it was not rerun during this port. No HA/feed/home-image/physical calls, installation or production changes were made. This is continuous JPEG preparation, not HLS/audio or hardware compatibility signoff. Version remains 1.0.60 until a separately authorized release candidate is packaged.
