# Addon1.0.60 candidate rollout plan — root execution only

This document does not execute maintenance. Candidate scope is c50f398 domain
reporting coverage plus exact live-target preflight. Do not include camera or SSE
candidate branches. Version1.0.60 is prepared once for CI; no live upgrade occurs
before compatible backend gates and exact-source image/tests pass.

## Packaging and admission gates

- Keep addon PR1 draft, no merge. Verify exact source/head and real image CI,
  including imports, offline version fallback and both SQLite reopen checks.
- Confirm compatible backend deployment completed successfully, existing failed/
  expired/unknown acknowledgements and optional missing dispatch evidence work,
  and the app truthfully rejects absent/unavailable targets. No rules apply is
  implied by this package. Root controls the current ordered rollout.
- Record current Supervisor HA/addon info, installed59 source, slug35ade3b8_lapahub,
  private-data mount identity, startup/watchdog/autoupdate settings, queue/outbox
  baseline and current authoritative receipt/scene retention manifest. Do not
  print credentials or rely on the old fixed eight-receipt count.

## Fresh stopped-consistent protection before installation

- Coordinate a quiet command window, stop the addon cleanly and verify stopped.
  No pump or other physical controls; only KM is authorized for physical validation.
- Capture fresh exact Advanced SSH configuration/protection/running state privately
  before any needed temporary access. Prior state was captured/restored/stopped,
  but do not execute old task JSON or assume old credentials/options are current.
- Resolve the actual persistent private /data mount. While stopped, verify both
  command_journal.sqlite3 and managed_scenes.sqlite3 using read-only integrity
  checks; capture schema and file/mount identities plus exact current receipt IDs,
  fingerprints/outcomes/trace times and all scene definitions/tombstones.
- Create a NEW stopped partial Supervisor backup using /backups/new/partial,
  addons:[35ade3b8_lapahub], homeassistant:false, compressed:true. Wait for real
  completion, retain its slug and archive SHA-256, and independently verify both
  database files and retention contents inside that backup. No backup name or
  HTTP success alone establishes its data contents.
- Preserve existing verified backup97d23dde as an additional rollback artifact.
  Never overwrite populated data with the original database-empty1.0.53 backup.
  Keep the actual59 image/source and the new backup available for rollback.

## Same-slug install and real retention checks

- Only after all gates pass, build/install60 under the SAME slug/private-data mount.
  Never uninstall/recreate it or reset options/data. No SQLite migration is needed.
- Attest actual installed version/source hashes/imports on ARM64, not store metadata
  alone. Verify running state and expected authentication/state/energy health.
- Recheck both SQLite identities/schema/integrity and the exact before-manifest
  receipt fingerprints/outcomes/dispatch times and scene tombstones. Preserve
  existing rows; do not manufacture retention from new boot log counts.
- Confirm startup did not execute tracked intents. Any actual KM validation must
  use a fresh explicitly authorized intent and preserve all prior receipt evidence;
  do not operate pool/camera/other hardware. Missing-target behavior uses generated
  or safely non-mutating preflight proof, never an unauthorized physical POST.
- On any retention/source/health failure, stop and capture the current database
  files and row manifest before recovery. Prefer the captured59 image against the
  unchanged compatible data mount. Never restore a pre-upgrade backup over newer
  receipts or scene tombstones: that could erase replay protection. If restoring
  data is necessary, preserve the latest durable history in a reviewed recovery
  plan first, then re-attest both databases. The new verified populated backup is
  a recovery artifact, not permission to discard post-backup writes.

Finally restore the exact captured Advanced SSH options/protection and stopped
state, preserve original addon startup/watchdog/autoupdate policy, and verify those
actual states. Retain a durable safe report linking CI, source, fresh backup hash,
installed attestation and before/after retention results. Root, not this plan or
a successful package build, authorizes and performs the live maintenance steps.
