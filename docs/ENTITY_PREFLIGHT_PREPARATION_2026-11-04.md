# Exact live-HA target preflight — preparation only

Root independently proved that the old cloud pool fan/switch entity IDs were
absent from complete HA states and entity-registry inventories, while different
current entity IDs existed. HA can return HTTP200 for service requests aimed at
nonexistent entities. Therefore HTTP service acceptance alone did not establish
that those old targets existed or changed. This source does not guess a new target
from names, parent labels or similar channels, and does not perform a binding repair.

Before each targeted call_ha_service POST, the addon now performs a direct
Supervisor-authenticated GET /api/states/{strictly encoded canonical entityId}.
It never trusts the discovery cache for dispatch. The response is bounded64KiB,
requires HTTP200/application-json/object/exact entity_id/nonempty state/attributes,
and rejects known unavailable or restored targets. Unknown is allowed for existing
stateless targets such as buttons/remotes and valid scene.turn_on requests.
404, wrong entity, invalid JSON/shape/MIME, redirects, oversized responses or
transport uncertainty are not dispatched. There is no fallback to homeassistant
wildcards or guessed replacement entity IDs.

The read budget is at most one second and the original remaining command deadline.
The deadline is checked again after asynchronous lookup and immediately before
POST; the POST timeout is also bounded by the original remaining budget. An
explicit new scene.create has no pre-existing target and is exempt from entity
lookup; existing target activations and automation services use the common guard.
No camera or SSE changes are included.

Task-local ContextVar metadata carries the logical command's journal/deadline
through common service calls without leaking between concurrent commands or
maintenance tasks. The existing trace JSON column/schema is unchanged. Before the
first service preflight, the legacy claim-time dispatch marker is removed. After
a valid lookup and deadline check, the first actual POST boundary receives its
precise dispatch timestamp. A rejection before any POST records existing outcome
failed with no addonDispatchedAt. Expiry before POST records expired. Network loss
AFTER a POST remains unknown, since physical effects cannot be inferred.

For multi-step commands, only the first preflight clears the premature marker;
a real first POST and its timestamp are never erased by a failed second preflight.
Such later failures/expiry report unknown with preserved dispatch evidence, never
claim the whole operation was not dispatched. Tests exercise both actual managed
scene restoration followed by missing activation and a two-service logical intent.

All85 tests on this exact branch pass, including the71 existing non-camera action
HTTP matrix with GET-before-POST order, strong negative lookup cases, unknown
stateless/scene targets, post-fetch expiry, one-second uncertain lookup, no retry
of failed intent IDs, concurrent deadline isolation, precise timestamp placement,
remaining-budget POST timeout, and preservation of historic receipt rows/schema
and the separate scene database/tombstone. The initial preflight regression run
failed nine assertions on the old source. Additionally, the four current PR1 hard
process-crash boundary tests were temporarily copied and run with this source:
all89 tests passed; the duplicate test file was removed afterward. No production
HA request, physical operation or live database access was performed.

The full-inventory absence protocol is a separate DOC-ONLY proposal. No snapshot
completion field, absence flag, binding change or legacy-domain invalidation is
implemented by this source. Existing client/backend failed outcomes remain
compatible, but exact-source image CI and root-controlled backend/addon gates are
required before runtime installation.
