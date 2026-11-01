# Addon live JPEG preparation — isolated, not deployed

This change implements the frozen cameraRelay v1 hub contract from the environment
outage handoff. It is preparation only: no production HA request, home camera or
image inspection, addon upgrade, version bump, database migration, rules apply or
main-worktree edit occurred. Existing generated-camera API evidence in af155434
is preserved separately and is not attributed to this new capture worker.

Camera discovery and realtime publication now include camera entities only after
strict attribute filtering: friendly_name <=200 characters, supported_features
nonboolean integer 0..2^31-1, motion_detection boolean. All other attributes are
dropped before local cache/publication, including HA access tokens, entity_picture,
stream sources, URLs and nested credentials. Camera device_class is null and state
is restricted to idle/streaming/recording/off/unavailable/unknown. The two actual
method regression tests failed before this change because camera was excluded.
Non-camera attributes keep their existing behavior. The backend must independently
apply the same allowlist before this writer is deployed.

CameraCaptureWorker runs as a bounded asynchronous background loop. Each iteration
reauthorizes via POST /hub/demand using the current existing hub-session Bearer.
Only up to four current camera.* view bindings are accepted. Capture concurrency
is two. No remembered lease is used after a failed demand; transient demand/relay
transport loss pauses capture until successful reauthorization. Explicit terminal
403/404/410/503 responses and HA off/error status block the view until a new view
ID. An absent demand drops its metadata immediately. An existing view ID cannot
rebind to a different camera.

HA requests use only the fixed configured HA base and encoded validated entity
path /api/camera_proxy/{entity}, requesting 640x360; this is a request, not a resize
guarantee. The worker never follows redirects, controls a camera, or uses an HA
URL from a lease. Download timeout is bounded by three seconds and remaining
lease time. JPEG bytes are bounded at 512 KiB even without Content-Length. Pure
Python bounded marker/SOF/SOS/EOI checks limit dimensions to 1920x1080/2073600
pixels; there is no Pillow or new native/runtime dependency and no full decode
claim. image/jpg is normalized to image/jpeg after validation.

The worker reserves a monotonically increasing RAM sequence and reconciles with
next demand.lastSequence. X-Lapa-Observed-At records completion of HA byte observation,
not sensor exposure time. Expiry is checked again after asynchronous capture.
Upload is binary POST /hub/frame with only hubId/viewId query selectors and the
frozen sequence/observation headers. HA credentials are sent only to HA; the current
hub credential is sent only to the relay. Unavailable/off/error reports use only
the fixed safe enum via /hub/status, never an exception body, URL or credentials.
No image bytes, leases or per-frame commands enter SQLite, logs, cloud documents
or disk caches. Synthetic checked-in test fixtures are not production frame caches. Local frame buffers are cleared after each attempt.

Validation: all 87 addon tests pass, including 18 new camera checks. The new tests
exercise real localhost aiohttp routes with static synthetic JPEG fixtures and a
relay stub, not production HA or the actual backend. They cover baseline/progressive
JPEG structure, malformed markers/dimensions, chunked oversized bodies, sequence
reconciliation/rate limits, missing/invalid demand, camera path injection/rebinding,
HA auth failure, camera-off 503, revocation during capture, expiry during a gated
capture, and four-view concurrency bounded to two. Cancellation while capturing
leaves both populated fixture databases' inode identities and SHA-256 bytes
unchanged; integrity, accepted receipt/no-replay and scene tombstone checks pass.
All application modules and image smoke compile; diff whitespace checks pass.

A further real localhost integration ran the actual new worker through the backend
cameraRelay production HTTP handler and Firestore emulator store, with the explicitly
synthetic demo-lapahub-camera-local identity boundary. A generated HA-shaped HTTP
source supplied two different fixture JPEGs. Viewer GET returned both advancing
hashes with sequences 1 and 2; virtual-source 503 cleared the relay and blocked
that generation; closing the view removed demand. The safe JSON evidence records
hashes/status only. An initial integration rejected the Python UTC timestamp format
with HTTP400; the worker now emits canonical UTC millisecond-Z headers and a
regression enforces that shape. This is not real Firebase authentication, official
HA instance, production ownership or native UI proof.

CI's existing real image smoke now additionally imports the camera module and
validates a generated JPEG fixture without adding image libraries. That CI job
has not been executed for this source yet. Local disk has about 4.5 GiB free while
this Docker vfs driver previously needed about 5.2 GiB for a scratch HA container;
no clone, driver change, cache deletion or disk-full risk was attempted.

Release gates remain: exact-source real image CI; actual new worker against cached
HA with generated-only camera assets; remaining permission/consent faults across the actual backend
helpers/emulator; app lifecycle/freshness proof; and
root-controlled backend-before-addon rollout with both production databases
preserved. Prior af155434 proves the HA JPEG/MJPEG API only. This commit does not
prove end-to-end live camera viewing, native performance or hardware compatibility.
