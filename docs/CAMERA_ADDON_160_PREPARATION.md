# Future camera addon 1.0.61 candidate based on installed 1.0.60

This isolated candidate starts at `1eea828` (installed code `455f939` plus rollback documentation). It ports the historical camera API evidence and camera-only implementation without importing the uninstalled SSE transport candidate.

`camera_live_enabled` defaults to false. Missing/false disables camera discovery, realtime publication and relay polling before worker initialization. Camera controls and scene membership remain rejected regardless of this flag. Enabling requires the separate backend/privacy/ownership rollout gate; this branch does not enable any live installation.

The relay URL is fixed. Hub and Supervisor credentials appear only in their respective Authorization headers; redirects are disabled. Each valid demand permits at most four views, two concurrent captures, bounded metadata, 512 KiB JPEGs and a monotonic lease deadline. Capture has no database/frame cache; database schemas, original command deadlines, target preflight and first actual dispatch evidence remain unchanged. Requested dimensions are advisory HA query parameters, not a claim that every integration resizes.

Validation on this preparation: combined 120 Python tests passed, including missing/false gate no-contact checks, camera privacy/control/scene guards, simulated HTTP capture/expiry/revocation/JPEG bounds, existing 71 service mappings, preflight failures, multi-service dispatch evidence and both database reopen/retention tests. Python compile and whitespace checks passed. Image build/import/private-database smoke is a separate CI gate, not yet claimed by this document.

`HA_CAMERA_API_2026-10-29.*` records historical generated isolated HA evidence; it was not rerun during this port. No HA/feed/home-image/physical calls, installation or production changes were made. This is continuous JPEG preparation, not HLS/audio or hardware compatibility signoff. Future version 1.0.61 packages the reviewed camera code with the false default unchanged; the live addon remains 1.0.60. Backend deployment and privacy review must precede any addon rollout, followed by the app gate.

The original code checkpoint `0107045` passed exact-source CI run `41811095317` (120 tests and Docker image smoke). Version 1.0.61 has a separate exact-source CI gate; the earlier run is not evidence for this new packaging commit. Backend incarnation fencing is server-internal and does not change the worker wire contract.

## Maintenance gate evidence — 2026-11-04

Version 1.0.61 checkpoint `3cb07e0c71c9a95dd2fee0a2ee39f5e36669ba17` passed exact-source CI `41821850600`: combined 120 tests, compile, and Docker image/import/JPEG/both-database smoke.

The maintenance operator subsequently ran seven synthetic archive-verifier tests in the cached addon 1.0.60 image as UID/GID 65534, with no capabilities, network or real database mount. All passed, including nonempty WAL, strict tar root-directory handling, negative archive fixtures and actual streamed-archive hash checks. Supervisor could not change UID/GID itself; the successful design starts the verification container directly as nonroot rather than adding privileges.

Fresh stopped backup `70785a3e`, SHA256 `5f751e06a831b29cef4f9169d34b3942a9523572a806a99be483d49e2ebf1dd7`, was verified through the bounded nonroot stream verifier against the fresh stopped baseline: all 48 receipts, one scene tombstone, both database schemas, integrity, database bytes and WAL/SHM state matched. Earlier backups remain retained; this evidence does not authorize restoring older receipt state over newer commands.

At this checkpoint, live addon 1.0.60 has been resumed, the store catalog still reports 1.0.60, and 1.0.61 has not been installed. Camera access remains disabled. The HA source checkout is clean at `3cb07e0`; the completed reload had no active git processes at the operator's 13:11 UTC read. Supervisor refreshes its store cache only when a repository reports a changed commit. This documentation-only child leaves all addon code/configuration unchanged so a later official reload can observe an ordinary upstream change. Do not manually advance the HA checkout to this child before that reload.

Installation remains gated on exact-source CI for this child, one official reload with observed completion/catalog version, fresh backup/retention evidence, and actual installed-source verification. This records operator-supplied maintenance evidence; it does not claim current home-camera feed or physical-camera compatibility validation.
