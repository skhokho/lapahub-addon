# Offline addon device-control matrix

This work starts at deployed78f96b9/addon1.0.59. It is separate from the camera,
SSE, app90 and backend fast-routing lanes. No production request, pool-pump or
other physical operation, addon update, version change or schema change occurred.
Only KM remains physically authorized; none of these tests operates even KM.

The matrix covers all71 existing backend-admitted actions across15 non-camera
domains: light, switch, input_boolean, siren, climate, cover, fan, lock,
media_player, remote, button, vacuum, water_heater, humidifier and valve. A copied
backend contract fixture is checked for exact action-set agreement. Camera's
legacy allowlist entry is deliberately excluded: its view-only privacy changes
are separately gated and must not be enabled by this source change. A native
pump domain is not invented or added to the backend allowlist.

Actual extracted execute_command, _execute_command_once and call_ha_service
methods call real localhost fake-HA HTTP routes. Each action checks its exact
canonical service path, entity selector and concrete typed parameter example,
then the real journal/acknowledgement method posts its result to a fake cloud
HTTP route. These are routing/payload simulations, not real HA service-schema,
entity-capability, hardware or production-authorization certification.

All71 routes already pass on the deployed source. Generic canonical services pass
through unchanged; the two legacy cover aliases open/close map to open_cover and
close_cover. Simulated switch.pool_pump and fan.pool_pump use their actual entity
prefix even when command.domain says light. Backend and addon both derive the
routing domain from entityId, so an app domain-field inconsistency alone does not
explain the actual pool-pump failure. A wrong service selected from a misclassified
type or unsupported entity capability can still fail; real command/receipt and
read-only entity metadata are needed to diagnose that device.

A concrete reporting defect is fixed: addon sync and realtime filters previously
excluded eight domains that the backend already admits: input_boolean, siren,
remote, button, vacuum, water_heater, humidifier and valve. Nine assertions failed
before the filter fix: eight missing realtime publications and the discovery-set
gap. They now pass. The change adds those non-camera domains to existing filters
without adding commands, services, privilege, physical behavior or unknown types.
Camera remains excluded, and existing sensor/binary-sensor/scene behavior remains.

State tests preserve the original HA last_updated change clock exactly through
actual state handling and fake-cloud HTTP publication; they do not re-stamp an
unchanged state as a new change. HA HTTP400/401/404/500/503 responses never produce
a service_accepted receipt. The existing uncertain outcome remains unknown, not
a fabricated definitive hardware result. Fake-cloud state503 returns failure for
publication retry; acknowledgement503 raises and leaves the accepted receipt in
the real outbox for retry. Source acceptance still is not physical confirmation.

All74 tests pass on this isolated branch:65 existing plus9 new test methods,
including71 action subcases. Compilation and whitespace checks pass. Actual HA
schema/platform tests, per-entity supported_features, native UI controls and actual
pool-pump causation are not proven. The app/backend contract agent owns canonical
UI selection/capability fixes. Root owns any compatible-reader/addon/app rollout;
this preparation neither deploys nor claims all physical devices now work.
