# Read-only review of deployed addon 1.0.59

This isolated change starts at deployed source 78f96b9 and contains no camera
work, version change, database/schema change, production request or deployment.
The user's uncertain controls and absent graphs are not attributed to these
source hazards without runtime evidence.

Root's direct Chromium ingress status at 03:07 UTC showed HA2026.10.4/addon1.0.59,
cloud credentials present, WebSocket present, token expiry next day01:32UTC,
no queued/active/dropped state publications, last device sync03:07:09 and last
energy report03:06:46, sync errors0 and cumulative energy errors3. A successful
last_energy_report advances only after reportEnergy HTTP200, so these observations
do not support a current reporting/authentication/preferences stall. Credential
presence and a socket object alone are not proof of current session authorization
or completed state subscriptions.

Two independently reproduced source hazards are corrected:

1. Classic command polling silently returned on every non200 response. After
three SSE errors the listener enters polling; rejected polls then reset its error
counter as though successful. It could remain unauthorized without reaching its
listener reauthentication policy. The poll now raises a bounded status-only error,
allowing the existing error/backoff/reauthentication policy to work. Successful
empty polls are unchanged. Six status subcases and an actual extracted listener
fallback test failed before; all pass now.
2. Energy preferences used an unbounded secondary WebSocket handshake and reads.
Both primary HA state subscription setup and energy reporting await this method.
A stalled greeting/auth/result could block both paths indefinitely. A ten-second
whole-operation timeout now returns the existing failure result, preserving the
cached preference snapshot/time. Shutdown cancellation propagates. The original
source fails four gated-hang subcases; the corrected method passes handshake,
greeting, auth, result, successful update and cancellation coverage. Other registry
WebSocket methods were not changed; their similar timeout gaps remain a separate
source-review concern, not an asserted runtime cause.

All 71 tests for this exact deployed-source branch pass, with compilation and
whitespace checks. Tests use synthetic mocked HTTP/WebSocket contracts and actual
extracted addon methods; no device, cloud session or queued command was mutated.
This is not live recovery validation and is not a recommendation to deploy for a
hypothetical cause.

Diagnostic semantics remain important: HA last_updated is a state/attribute change
clock, not proof that unchanged values were freshly observed. Backend state ordering
correctly rejects equal/older HA change clocks; do not replace them with wall time
to manufacture command confirmation. Service acceptance alone is not a physical
result. Power qualification instead uses last_reported within600s, preserves fresh
reported zero, and marks unproven source freshness unmeasured. A fresh report may
therefore contain unavailable channels and no truthful power trace. Check actual
channel availability and canonical sample timestamps separately from addon uptime.

Backend issueHubSession rotates the session on each authentication. Local ingress
expiry/presence cannot prove a matching backend generation; safe comparison of
server connectedAt/expiresAt with addon status can help without reading credentials.
Current fresh successful energy delivery weighs against stale auth at that time.
