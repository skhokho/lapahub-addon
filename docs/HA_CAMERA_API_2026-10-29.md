# HA 2026.10.4 camera API capability probe

A real isolated instance of the cached official HA 2026.10.4 image served known
synthetic camera bytes through its existing authenticated camera APIs. No home
camera, external image URL, family image or physical device was accessed. No
production camera implementation or addon discovery change was made.

Before HA startup, the scratch container's four local demo JPG/PNG assets were
replaced with generated 160×96 checker patterns and a moving colored stripe.
The official DemoCamera and HTTP/WS implementation code remained unchanged.
DemoCamera cycles these local files; the generator's hashes match every served
JPEG byte-for-byte. The committed generator was also executed and reproduced
all hashes exactly. No image content is stored in logs or the JSON evidence.

| Actual API check | Observed result |
| --- | --- |
| Bearer GET /api/camera_proxy/{fixture_camera} | 200, image/jpg, valid JPEG, 160×96, known pattern hash |
| Bearer GET /api/camera_proxy_stream/{fixture_camera} | 200, multipart/x-mixed-replace, known JPEG frames with advancing hashes |
| No auth, snapshot and MJPEG | 403 |
| Invalid Bearer, snapshot | 401 |
| Virtual camera off, authenticated snapshot | 503 |
| Virtual camera on again | 200, known generated JPEG |
| WS camera/capabilities | advertises hls |
| WS camera/stream, format hls | start_stream_failed: demo has no actual stream source |

The multipart probe received three frames: the first two were identical, as HA
intentionally repeats its first frame for browser rendering; the third had a
different known hash. All frames were about 1 KiB. The fixture reader enforced
a 512 KiB per-frame limit and validated JPEG magic, decoded dimensions, MIME and
known local hashes. This is a capability check, not adversarial parser or large
image coverage. Only JPEG was served in the probe; generated PNG assets were not
claimed as API coverage.

There is an important framing compatibility detail: HA 2026.10.4 sends
Content-Type `multipart/x-mixed-replace; boundary=--frameboundary` but its wire
delimiter is `--frameboundary`, without another two-dash prefix. The initial
standard aiohttp MultipartReader waited for the RFC-prefixed delimiter and timed
out. A small bounded fixture reader accepting the observed delimiter then
verified actual frame bytes. This changes only the probe, not production parsing.
The fixture also returns image/jpg rather than image/jpeg; safe normalization
would require validating the actual bytes. No production MIME policy is chosen
by this evidence.

HLS capability advertisement alone did not establish a playable stream. The
official demo advertises the STREAM feature but does not implement a stream
source, and its actual camera/stream request fails. No HLS or WebRTC media bytes
were obtained. The proven path here is authenticated JPEG and MJPEG from local
changing images, not native video transport.

The exact HA CameraView accepts either an authenticated request or a camera
query token. Missing credentials return 403; an invalid Bearer returns 401. A
future LapaHub design must independently authorize its owner/device view and
must not expose HA camera access tokens, entity_picture URLs or stream source
credentials to clients. This probe does not implement or validate that security
architecture. Current addon relevant_domains excludes camera, so discovery was
not enabled by this work.

Both scripts are under scripts/protocol. In a fresh controlled fixture, generate
assets with generate_camera_patterns.py before starting HA, save its JSON as
/fixture/patterns.json, then run isolated_camera_api.py inside that instance.
The minimal configuration enables homeassistant, http, api, websocket_api,
config, onboarding, logger and the official virtual demo suite. It does not load
default_config or network discovery integrations. The fixture marker and
container check protect against accidental use outside the scratch setup.

The instance used an internal Docker network and localhost-only port binding,
with no host networking, home credentials, pulls, grants or external reachability.
The fixture account/token/configuration, container and network were removed.
Only safe hashes, dimensions, content types and result metadata remain. Whole-HA
offline/retry behavior, app viewing, cloud ownership enforcement, native playback,
production camera quality and hardware compatibility remain unverified.
