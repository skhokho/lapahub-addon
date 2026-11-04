# Isolated HA climate, lock and media protocol evidence

On 29 October 2026, the already cached official HA 2026.10.4 image ran one batch
of three local virtual-demo cases through the actual addon executor. The JSON
records the exact image digest, addon source revision/hash, individual intent
IDs, fingerprints, receipt traces and fresh HA WebSocket event times.

| Case | Fresh baseline | Existing action and parameters | HA WS confirmation |
| --- | --- | --- | --- |
| Climate | temperature 20 | set_temperature, temperature 21 | temperature 21 |
| Lock | locked | explicit unlock, no parameters | unlocked |
| Media | volume_level 1.0 | volume_set, volume_level 0.37 | volume_level 0.37 |

Schema review preceded dispatch. The Dart DeviceCommands temperature helper
uses serviceData.temperature; the lock provider chooses explicit lock/unlock.
The generic device repository forwards serviceData and the backend converts it
to addon params, allowing these actions in commandDelivery CONTROL_ACTIONS.
For media, the backend permits volume_set, the official demo accepts normalized
volume_level 0..1, and the app renderer reads that normalized attribute. This
checks the supported generic command path; no dedicated media volume UI or
native interaction was exercised.

The tested methods execute_command, _execute_command_once and call_ha_service
were compiled directly from the recorded addon source, using the real
CommandJournal. No cloud client was attached. Every case chose a target different
from a fresh HA read, used a new unique intent ID, and required service_accepted.
A WS subscription existed before dispatch, and each confirmation event had
HA time_fired at or after dispatch. Each receipt survived a real journal close
and reopen. An independent read of the retained safe journal confirms exactly
three matching ID/fingerprint/service_accepted rows and SQLite integrity_check=ok.
No case failed and no production code or confirmation tolerance changed.

Camera imagery was deliberately skipped. The app CameraCardRenderer identifies
its preview as a placeholder, and no implemented addon/app snapshot or streaming
client path was found. A fabricated HA camera endpoint would not verify that
missing product path. No external image requests, home or family images were
accessed. The backend's generic camera control allowlist is not camera image
support and was not tested by this batch.

The fixture used an internal Docker network and localhost-only port binding,
without host networking, home credentials, physical integrations or external
connectivity. Its minimal configuration enabled homeassistant, http, api,
websocket_api, config, onboarding, logger and the official virtual demo suite;
no default_config or network discovery integration was configured. An ephemeral
fixture account authenticated loopback REST and WS requests. No image was pulled
and no network grants were added.

The harness is scripts/protocol/isolated_domains.py, run inside this controlled
fixture with reviewed run.py and reliability.py copied into /fixture, using the
same scratch setup as the preceding cover case. It never reads home credentials.
Only safe result evidence and the three-command journal are retained. Container,
internal network and scratch configuration/account/token files were removed.

This evidence covers HA service/state protocol compatibility for these exact
virtual cases. It does not establish real thermostat/lock/media behavior, native
app UX, hardware safety, cloud authorization/ownership routing, reconnect
reliability for these domains, arbitrary capabilities or production readiness.
Existing offline durability tests and the authorized KM/scenes runtime evidence
remain separate evidence.
