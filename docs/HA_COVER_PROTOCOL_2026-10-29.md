# Isolated HA cover protocol observation — 29 October 2026

The official `ghcr.io/home-assistant/home-assistant:2026.10.4` image pulled
successfully through the configured session proxy, CA and enforced policy.
Image digest and addon source hash are recorded in the adjacent JSON evidence.
The image reports HA 2026.10.4 and contains `components/demo/cover.py`.

A scratch HA instance used an internal Docker network, a localhost-only
`127.0.0.1:18123:8123` binding, and no home credentials, host networking or
physical integrations. Its configuration enabled only homeassistant, http, api,
websocket_api, config, onboarding, logger and the official virtual demo suite.
No default_config or network discovery integration was configured. The case
client ran inside this container over loopback; its HTTP client ignored proxy
environment for this local connection. External image acquisition used the
unchanged managed Docker network configuration. No external services were
reachable from the internal fixture network.

The actual addon `execute_command`, `_execute_command_once` and `call_ha_service`
methods were compiled directly from the recorded source, with the real
CommandJournal. No cloud client was attached. An ephemeral fixture onboarding
account supplied local REST/WS authentication.

The first command requested numeric position 37. HA accepted it, and the real
journal reported service_accepted. An assertion waiting for WS position 37
expired: the official demo rounds set positions to tens. This is a fixture
expectation failure, not evidence of an addon defect or physical cover behavior.
No production confirmation tolerance was changed.

Repeating the same cover protocol case with supported target 60 (different from
current 40) passed: WS subscription was established before actual addon dispatch,
HA accepted the service, the journal reported service_accepted, and a subsequent
state_changed event reported current_position 60.

Evidence limitation: each observed run used a separate temporary journal and the
same fixture command ID. The original 37 receipt/fingerprint was deleted on test
exit. No ID was changed inside one journal, but these observations do not prove
a durable cross-attempt contract, retained original fingerprint, or journal
reopen behavior. The current small reproduction script fixes this by using a
unique ID for each new intent, retaining its fixture journal and a receipt
artifact, and checking a reopened journal. A fresh cached-image instance then ran that corrected harness once with target
40, different from its initial position. The actual executor received service
acceptance; the pre-established WS subscription observed current_position 40.
Closing and reopening the real journal retained the unique new intent's receipt.
The copied safe SQLite journal contains exactly one row with the expected ID,
fingerprint and service_accepted outcome, and passes integrity_check. The JSON
records its SHA-256, safe receipt fields and trace. This fresh run validates the
corrected harness; it cannot recover the original 37 receipt. Both initial and
corrected runs' evidence limits remain explicit.

Each HA container, internal network and scratch configuration/account/token files
were removed. Only safe result evidence and the corrected run's fixture command
journal were retained; the journal contains no HA authentication material. The official image remains cached. No production source, version,
deployment, app release, home configuration or physical device was changed.
This is one HA service/state-event protocol observation. It does not establish
physical-device compatibility, all cover percentages, climate/lock/media/camera
coverage, cloud routing, or app UX readiness.

For a controlled repeat, provision the same image and minimal scratch
configuration above on an internal network; verify image version and installed
demo cover source first. Copy the reviewed `run.py` and `reliability.py` into
`/fixture` and execute `scripts/protocol/isolated_cover.py` inside that fixture.
The script requires the fixture configuration marker and never reads home
credentials. Retain the safe receipt artifact and result before cleaning the
container, network and all scratch authentication material. Do not run against an
existing HA configuration or mounted addon data.
