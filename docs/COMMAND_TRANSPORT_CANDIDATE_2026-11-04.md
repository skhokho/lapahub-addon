# Next-milestone command transport candidate — offline only

This isolated candidate starts at 4d76d34, which itself starts at deployed addon
78f96b9/1.0.59. It contains no camera, app90, version, database schema, endpoint,
production configuration or deployment change. No SSH, restart, addon upgrade,
production request or device operation was performed.

The source defect is concrete: three SSE failures set use_sse=false, and successful
classic polls reset the error counter indefinitely. SSE is never retried unless
polling fails enough to reauthenticate. Independent token refresh also does not
restore SSE while successful polling continues. Repeated immediate clean EOFs had
no pacing because normal return reset the counter every iteration.

These defects are not established causes of the actual command timings. Root's
latest direct status shows a healthy1.0.59 process, and its old activity ring has
evicted transport history. The actual root queue, addon attempt and end-to-end
intervals do not isolate queue wait. Obtain pending.claimedAt versus queuedAt and
bounded transport request metadata before attributing the remaining delay to
classic polling. This candidate is not an addon deployment recommendation and is
not a blocker for the independent90 UI hotfix.

Recovery is deliberately serial. After completed successful classic polls, a
monotonic30-second deadline schedules an SSE connection/header attempt. Failed
retries back off30/60/120/240/300 seconds, capped300, while intervening classic
polls continue. A changed in-memory token generation resets the next attempt after
the current poll completes; no token or fingerprint enters status or logs. There
is no parallel SSE preclaim, overlapping command consumer or cancelled in-flight
poll. A background header-only SSE probe was rejected because that endpoint can
claim commands before handoff, stranding them while polling still waits.

SSE connection plus response headers have a configured one-second budget. Once
headers succeed, normal SSE reads keep their existing45-second inactivity and
130-second total budgets. Responses close on non200, EOF, exception and shutdown.
Short clean sessions under30 seconds count as failed and are paced into fallback;
normal healthy server rotation can reconnect normally. Missing credentials wait
for authentication rather than counting successful empty polls. Existing five-poll
error recovery still reauthenticates; existing HTTP rejection handling from4d stays.

A network failure after server claim can still strand an intent until its original
lease/deadline. This is an honest transport-loss possibility, not permission to
replay with a new ID or extend the deadline. A generated localhost fault claims a
command, withholds headers until the open times out, then redelivers the now-expired
intent through the actual poll/executor. The real journal records expired and the
HA service mock is never called. No journal reset or queue/receipt identity change
is introduced.

Ingress /api/status adds command_transport: mode, HTTP connection state, SSE/poll
counters, reauthentication attempts, token-generation-change count, safe error
class/time, last HTTP status/contact time, fallback time and monotonic retry
countdown. These are current-listener-run transport observations, not physical
execution evidence or authentication proof. No credentials, URLs, command IDs,
queue contents or frames appear. Existing activity-ring eviction cannot remove
these in-memory fields. They reset on process/listener restart.

All81 addon tests pass. Four recovery/EOF regressions failed against the original
4d source before the change. Ten new tests cover continued healthy polling,
30-to300 backoff, token changes, no cancellation/overlap of a gated poll, missing
credentials, paced clean EOF, real HTTP hung headers, late expired claims,
post-header reads surviving the one-second opening budget, and cancellation that
releases sockets without closing the shared session. The local hung-header check
completed within1.4 seconds including scheduling/cleanup tolerance; it is not a
universal network latency guarantee. Existing device, scene, outbox and preference
hazard tests remain green. Compilation and whitespace checks pass.

Before runtime use, require exact-source image CI, a generated HA/backend transport
integration and review of observed production claim/request timings. Confirm the
one-second opening budget is suitable under actual managed network/cold-start
conditions. Root owns any eventual rollout with both databases preserved. No
merge into addon PR1 or app90 is authorized by this preparation record.

## Subsequent read-only transport evidence and optional warmup review

Root's exact-hub ten-minute Cloud Logging probe41772286511/job126384515944 at
04:18:54UTC observed432 completed getPendingCommands requests, all HTTP200,
zero completed waitForCommands requests, four unscoped records and no truncation.
Request durations were74.6–469.7ms and observed classic-GET cadence about1.4seconds.
This proves active classic-GET traffic during that window, not the component's
complete transport state or a2.5-second causal queue wait. The actual claimedAt
versus queuedAt interval and the remaining acknowledgement/UI stages still matter.
The independent90 upload/Apple-processing milestone remains unaffected.

An optional next design step is a nonclaiming, unauthenticated OPTIONS warmup
before a bounded serial SSE GET, if exact deployed wrapper behavior is verified.
The inspected source has no explicit waitForCommands OPTIONS branch/cors:true;
its no-Authorization path rejects before authentication/claim. Do not assume a
204 preflight or that a warmup reaches the same instance. Even a verified wrapper
preflight may warm initialization without proving the subsequent GET's claim or
Firestore path is warm. This is a proposal only; no OPTIONS request or source
implementation was added. A cold GET can still claim after client cancellation;
this candidate bounds the opening wait but cannot promise delivery of every
claimed intent before its original deadline. Never reset identities/deadlines or
replay expired physical commands to hide that transport loss.
