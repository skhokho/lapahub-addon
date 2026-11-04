# Proposal only: authoritative complete-inventory availability

No inventory writer, endpoint or invalidation behavior is implemented here.
Current78f legacy syncs must remain non-authoritative for absence: they omit eight
backend-supported domains. A missing row in that payload must never mean a device
was removed, and names must never select replacement entities.

## Proposed admission and ownership

Use compatible optional metadata on syncDevices, admitted only after the backend
verifies the existing hub-session Bearer and canonical owner/hub binding. The
backend, not caller owner IDs, stamps a server-only inventory receipt with canonical
owner/hub, authenticated session generation, receive time and validated coverage.
Clients cannot mint this receipt or set unavailable flags directly. This is a
backend-authenticated ownership receipt, not proof that HA cryptographically signed
the inventory or that the physical hardware is reachable.

Proposed envelope (field names subject to shared review):
- inventoryVersion:1; complete:true; observationId UUID;
- generation: server-issued authentication generation, bound to the exact current
  session; observationSequence: positive safe integer;
- observedAt UTC ISO: HA fetch observation time, never retry/send time;
- coveredDomains: explicit sorted bounded domain set for this capable writer;
- reportedCount and optional payload digest, checked against the actual unique
  canonical entity IDs in the submitted list.

Completeness is allowed only after a successful bounded HTTP200 HA /api/states
fetch, a valid full array, successful parsing of every covered-domain row, no
truncation or paging omission and no exceeded size/count limit. A parser/transport
failure cannot submit complete:true, even if an old local cache exists. Declared
coverage must be a subset of the backend-approved writer capability/domain set,
not inferred from nonempty rows. A domain may legitimately be covered and empty.
Camera must remain uncovered until its separately approved safe metadata writer.

## Ordering requirements before absence can be trusted

Use a server-issued session generation and a monotonic observation sequence shared
by full snapshots and realtime entity presence observations. Reserve a full-fetch
sequence at fetch start, so realtime observations during the fetch sort after it.
A restarted addon authenticates into a new generation; no SQLite schema or image
persistence is needed. Freeze generation/token for the operation: session refresh
mid-fetch must cause the old generation to be rejected/retried, not relabel an old
snapshot with a new generation or observation time.

The backend rechecks current session generation inside the inventory commit
transaction and rejects stale generations, duplicate/older snapshot sequences,
invalid timestamps and future/out-of-window observations. Exact UTC age/skew limits
need shared agreement; sequence/generation, not wall-clock timestamp alone, is the
ordering authority. A late old full-sync HTTP response must never invalidate a
newer realtime-presence observation. Every affected entity therefore needs the
same generation/observation-order fence, and transactions must retry if that fence
changes concurrently. Generation ordering must be server-defined; client UUID
lexical order is not chronological authority.

## Narrow absence effect

Only mark a currently authorized canonical entity absent when all apply:
- this is an admitted complete receipt for its exact domain;
- its exact canonical entity ID is absent from the validated list;
- no newer presence/inventory observation exists for that entity;
- the session, owner/hub binding and commit generation remain current.

Record a separate availability reason such as missing_from_complete_inventory,
with the receipt/generation/order and observation time. Do not delete or rebind
the device, alter authorization/room/name/service configuration, reset receipts,
edit scenes or infer a same-name replacement. A disabled or unloaded registry entry
can be absent from states without being permanently deleted; label current target
unavailability, not historical hardware removal. Live exact-entity command
preflight remains mandatory even after this availability metadata exists.

Clear the absence flag only from a newer authoritative exact-ID presence report,
not any unrelated updatedAt write or legacy partial sync. Legacy writers and
unknown domains may continue their compatible updates but cannot invalidate or
resurrect authoritative absence. Registry stable identity may inform an explicit
owner-reviewed repair; this protocol must not perform that repair automatically.

## Required rollout and tests

Compatible readers/auth generation/capability admission first; only then capable
addon writer; only after verified complete receipts may absence affect controls.
Until every required guard exists, collect dry-run receipts/counts only and do not
mark devices missing. Existing78f payloads never opt in by inference.

Mandatory emulator/generated-source tests: empty covered domain; partial legacy
sync; omitted eight domains; truncated/invalid full response; duplicate IDs/count
mismatch; timestamp skew; two out-of-order full snapshots; old response after token
rotation/restart; newer realtime presence racing an older snapshot; concurrent
revocation; no cross-owner changes; exact-ID reappearance; disabled registry entry;
and no automatic same-name rebinding or scene/receipt mutation. Shared backend/
addon/root review must freeze the wire contract before implementing any of this.
